"""
Utilities to parse iReal Pro chart data and extract chord annotations.
The implementation currently leverages `pyRealParser`.

"""
import os
import re
import glob
import copy
import urllib
import logging

import jams
from matplotlib.pyplot import new_figure_manager
import numpy as np
import pandas as pd
from pyRealParser import Tune

from utils import create_dir, pad_symbol
from jams_utils import append_metadata
from jams_score import append_listed_annotation

logger = logging.getLogger("choco.ireal_parser")

IREAL_RE = r'irealb://([^"]+)'
IREAL_NREP_RE = r"{.+?N\d.+?}"  # to identify all complex repeats
IREAL_REPEND_RE = r"([{\[|]?)\s*N(\d)"  # to identify all ending markers
IREAL_CHORD_RE = r'(?<!/)([A-Gn][^A-G/]*(?:/[A-G][#b]?)?)'  # an iReal chord


def mjoin(chord_string:str, *others):
    """
    Metrical join between chord strings, inserting a measure symbol "|" among 
    consecutive chord strings only if a (bar) separator is missing. Everything
    will look like this: "string_a | string_b | ... | string_z".
    """
    pre_chords = [cs for cs in [chord_string]+list(others) if cs.strip() != ""]
    merged_chords = [pre_chords[0]]  # take first non-empty string
    for next_cstring in pre_chords[1:]:
        # print(next_cstring + "\n\n")
        separator = "|" if merged_chords[-1].rstrip()[-1] != "|" \
            and next_cstring.lstrip()[0] != "|" else ""
        merged_chords.append(separator+next_cstring)

    merged_chords = "".join(merged_chords)
    return merged_chords


class ChoCoTune(Tune):

    @classmethod
    def _insert_missing_repeat_brackets(cls, chord_string):
        """
        Handle implicit curly brackets and insert one at the beginning of the
        chord string, where the repetition is trivially expected to start.
        """
        if chord_string.count("{") != chord_string.count("}"):
            logger.warning("Uneven number of curly brackets in chord string")
        # Evening out repeating bar lines for consistent expansion
        cbracket_opn_loc = chord_string.find("{")
        cbracket_cld_loc = chord_string.find("}")

        if cbracket_cld_loc > -1 and (cbracket_opn_loc == -1 \
            or cbracket_opn_loc > cbracket_cld_loc):
            # Assume that there is a leading open bracket missing
            logger.warning("Inserting leading '{' to compensate")
            chord_string = '{' + chord_string

        return chord_string

    @classmethod
    def _fill_long_repeats(cls, chord_string):
        """
        Replaces long repeats with multiple endings with the appropriate chords.

        Parameters
        ----------
        chord_string : str
            The chord string following cleanup, insertion of missing brackets,
            and (optionally but preferably) removal of extra comments besides
            those wrapping explicit rounds of repetition.
        
        Returns
        -------
        new_chord_string : str
            A new chord string resulting from the expansion of repetitions of
            two kinds: complex repetitions with different endings (marked by N1,
            N2, etc. symbols), and full bracketed repetitions where an optional
            special comment may be used to indicate the number of repeats.

        """
        repeat_match = re.search(r'{(.+?)}', chord_string)
        if repeat_match is None:
            return chord_string
        full_repeat = repeat_match.group(1)

        # Check whether there is a first ending in the repeat
        number_match = re.search(r'N(\d)', full_repeat)
        if number_match is not None:
            # Sanity check and verification of additional numbered repeats
            macro_repeats = list(re.finditer(IREAL_NREP_RE, chord_string))
            logger.info(f"Found {len(macro_repeats)} complex repeat(s)")
            current_mrstart = macro_repeats[0].start()  # of this macro repeat
            current_bnd = macro_repeats[1].start() \
                if len(macro_repeats) > 1 else len(chord_string)
            assert current_mrstart < current_bnd, \
                f"Illegal substitution: current macro repeat starts at " \
                f"{current_mrstart}, next macro (or end) at {current_bnd}"
            # Now, get rid of the first repeat number and the curly braces
            first_repeat = re.sub(r'N\d', '', full_repeat)
            logger.info(f"Resolving first marked repeat in {full_repeat}")
            new_chord_string = mjoin(
                chord_string[:repeat_match.start()], \
                first_repeat, chord_string[repeat_match.end():])
            # Remove the first repeat ending as well as segnos and codas
            repeat = cls._remove_markers(
                re.search(r'([^N]+)N\d', full_repeat).group(1))
            # Find the next ending markers and insert the repeated chords before
            for _ in re.findall(IREAL_REPEND_RE, new_chord_string):
                mrep = lambda x: x.group(1) + repeat \
                        if x.start() < current_bnd else x.group(0)
                new_chord_string = re.sub(
                    IREAL_REPEND_RE, mrep, new_chord_string)

        else:  # Bracket repeat: which can either be performed twice or more
            to_repeat, times = full_repeat, 2  # defaul case (brackets only)
            # Check whether the number of repeats is explicitly annotated
            no_repeats_match = list(re.finditer(r'<(\d+)x>', full_repeat))
            if len(no_repeats_match) > 0:  # explicit repeats provided
                times = int(no_repeats_match[-1].group(1))  # use last marker
                s, e = no_repeats_match[-1].start(), no_repeats_match[-1].end()
                to_repeat = full_repeat[:s] + full_repeat[e:]
                to_repeat = to_repeat.replace("  ", " ")
            # Unroll th repetition but keep the first occurrence with markers
            logger.info(f"Times {times} repeat of {to_repeat}")
            repetitions = (' |' + cls._remove_markers(to_repeat)) * (times-1)
            new_chord_string = mjoin(
                chord_string[:repeat_match.start()], \
                to_repeat, repetitions, \
                chord_string[repeat_match.end():])  # + '|'

        # There could be other repeat somewhere, so we need to go recursive
        new_chord_string = cls._fill_long_repeats(new_chord_string)
        return new_chord_string

    @classmethod
    def _fill_single_double_repeats(cls, measures):
        """
        Replaces 1- and 2-measure repeat symbols with the appropriate chords:
        'x' repeats the previous measure, 'r' repeates the former two. 

        Parameters
        ----------
        measures : list of str
            A list of measures, eacnh encoded as a string.

        Returns
        -------
        new_measures : list of str
            A new list of measures where 'x' and 'r' are infilled.

        """
        pre_measures = []
        for measure in measures:  # marker has its own parenthesis
            splits = re.split(r"[rx]", measure)
            markers = re.findall(r"[rx]", measure)
            measures_xt = [val.strip() \
                for pair in zip(splits, markers+[""]) \
                for val in pair if val.strip() != ""]
            pre_measures += measures_xt

        new_measures = []
        # Add extra measures for double repeats
        for i, measure in enumerate(pre_measures):
            if measure == 'r':  # just prepare the ground for later
                new_measures.append(measure)
                if i+1 == len(pre_measures) or pre_measures[i+1].strip() != "":
                    new_measures.append(" ")  # empty slot needed for r
            else:  # anything else is kept as it is
                new_measures.append(measure)

        # 1- and 2-measure repeats: safe now
        for i in range(1, len(new_measures)):
            if new_measures[i] == 'x':  # infill the 1-m repeat from last bar
                new_measures[i] = cls._remove_markers(new_measures[i-1])
            elif new_measures[i] == 'r':  # infill the 2-m repeat from last two
                new_measures[i] = cls._remove_markers(new_measures[i-2])
                assert new_measures[i+1].strip() == "", \
                    f"Illegal repeat: non-empty bar ahead: {new_measures[i+1]}"
                new_measures[i+1] = cls._remove_markers(new_measures[i-1])

        return new_measures

    @classmethod
    def _fill_slashes(cls, measures):
        """
        Replace slash symbols (encoded as 'p') with the previous chord.

        Parameters
        ----------
        measures : list of str
            A list of chord measures (strings) where single and double repeats
            ('x' and 'r') have already been infilled in previous stages.

        Returns
        -------
        new_measures : list of str
            A new list of measures with filled slashes.

        """
        chord_regex = re.compile(IREAL_CHORD_RE)

        for i in range(len(measures)):
            while measures[i].find('p') != -1:
                slash = measures[i].find('p')
                if slash == 0:  # slash in 1st position needs measure lookback
                    prev_chord = chord_regex.findall(measures[i - 1])[-1] + " "
                    measures[i] = prev_chord + measures[i][1:]
                    measures[i] = re.sub(r'^(p+)', prev_chord, measures[i])  # ?
                else:  # repeating a chord that should be found in the same bar
                    prev_chord = chord_regex.findall(measures[i][:slash])[-1]
                    measures[i] = measures[i][:slash] + \
                        prev_chord + " " + measures[i][slash + 1:]

        return measures

    @classmethod
    def _clean_measures(cls, measures):
        """
        Post-processing chord symbols to make sure iReal-specific markers end up
        separated from the chordal information. This includes, no-chord symbols
        (N.C. encoded as concatenated 'n' markers) and ...

        Parameters
        ----------
        measures : list of str
            A list of measures at the final stage of pre-processing.
    
        Returns
        -------
        new_measures : list of str
            A new list of measures where all chords are individually readable.

        """
        # chord_regex = re.compile(IREAL_CHORD_RE)
        new_measures = copy.deepcopy(measures)
        for i in range(len(new_measures)):
            while new_measures[i].find('n') != -1:  # safe with replace
                new_measures[i] = pad_symbol(new_measures[i], "n", "N")

        for i in range(len(new_measures)):  # final pass to remove extra
            measure_tmp = new_measures[i]
            measure_tmp = re.sub(r"[US]", "", measure_tmp)
            measure_tmp = re.sub(r"\s\s+", " ", measure_tmp).strip()
            # re.sub(r'U|S|Q|N\d', '', measure)  # TODO
            new_measures[i] = measure_tmp

        return new_measures

    @classmethod
    def _remove_unsupported_annotations(cls, chord_string):
        """
        Removes certain annotations that are currently not handled/used by the
        parser, including section markers, alternative chords, time signatures,
        as well as those providing little or none musical content.
        """
        # unify symbol for new measure to |
        chord_string = re.sub(r'[\[\]]', '|', chord_string)
        # Remove empty measures: safe because of "n" and "p"
        chord_string = re.sub(r'\|\s*\|', '|', chord_string)
        # Remove comments except explicit repeat markers (<3x>)
        chord_string = re.sub(r'(?!<\d+x>)<.*?>', '', chord_string)
        # remove alternative chords
        chord_string = re.sub(r'\([^)]*\)', '', chord_string)
        # remove unneeded single l and f (fermata)
        chord_string = re.sub(r'[lf]', '', chord_string)
        # remove s (for 'small), unless it's part of a sus chord
        chord_string = re.sub(r'(?<!su)s(?!us)', '', chord_string)
        # remove section markers
        chord_string = re.sub(r'\*\w', '', chord_string)
        # remove time signatures
        chord_string = re.sub(r'T\d+', '', chord_string)

        return chord_string

    @classmethod
    def _get_measures(cls, chord_string):
        """
        Split a chord string into a list of measures, where empty measures are
        discarded. Cleans up the chord string, removes annotations, and handles
        repeats & codas as well.

        Parameters
        ----------
        chord_string: str
            A chord string as originally encoded according to iReal's URL.

        Returns
        -------
        measures : list
            A list of measures, with the contents of every measure as a string.

        """
        chord_string = cls._cleanup_chord_string(chord_string)
        chord_string = cls._insert_missing_repeat_brackets(chord_string)
        # Time to remove unsupported annotations, and improve consistency
        chord_string = cls._remove_unsupported_annotations(chord_string)
        # Unrolling repeats with different endings and coda-based
        chord_string = cls._fill_long_repeats(chord_string)
        chord_string = cls._fill_codas(chord_string)
        # Separating chordal content based on bar markers
        measures = re.split(r'\||LZ|K|Z|{|}|\[|\]', chord_string)
        measures = [m.strip() for i, m in enumerate(measures) \
            if m.strip() != '' or measures[i-1].strip() == "r"]  # XXX n.n.
        measures = [m.replace("U", "").strip() for m in measures]
        # Infill measure repeat markers (x, r) and within-measure (p)
        measures = cls._fill_single_double_repeats(measures)
        measures = cls._fill_slashes(measures)
        measures = cls._clean_measures(measures)

        return measures

    @staticmethod
    def parse_ireal_url(url):
        """
        Parse iReal charts (URL) into human- and machine-readable formats.

        Parameters
        ----------
        url : str
            An url-like string containing one or more tunes.

        Returns
        -------
        tunes : list
            A list of ChoCoTune objects resulting from the parsing.
        pname : str
            The name of the playlist if the given charts are bundled.

        """
        url = urllib.parse.unquote(url)
        match = re.match(IREAL_RE, url)
        if match is None:
            raise RuntimeError('Provided string is not a valid iReal url!')
        # Split the url into individual charts along the '===' separator
        charts = re.split("===", match.group(1))
        charts = [c for c in charts if c != '']

        tunes, pname = [], None
        for i, chart in enumerate(charts):
            if i == len(charts)-1 and "=" not in chart:
                pname = chart.strip(); break  # fermete
            try:  # attempt parsing of the individual tune
                tune = ChoCoTune(chart)
                tunes.append(tune)
                logger.info(f"Parsed tune {i}: {tune.title}")
            except Exception as err:
                logger.warn(f"Cannot import tune {i}: {err}")

        return tunes, pname


def extract_metadata_from_tune(tune: ChoCoTune, tune_id=None):
    """
    Extract metadata information from an iReal tune, an object resulting from
    parsing an original chart with the `parse_ireal_url` function.

    Parameters
    ----------
    tune : ChoCoTune
        The ChoCoTune instance from which metadata needs to be extracted.
    tune_id : str
        An optional string that can be used for indexing the tune in the dict.
    
    Returns
    -------
    metadata : dict
        A dictionary providing the metadata extracted from the given tune.

    """
    metadata = {
        "id": tune_id,
        "title": tune.title,
        "artists": tune.composer,
        "genre": tune.style,
        "tempo": tune.bpm,
        "time_signature": tune.time_signature,
    }

    return metadata


def extract_annotations_from_tune(tune: ChoCoTune):
    """
    Extract chord and key annotations from a given tune.

    Parameters
    ----------
    tune : ChoCoTune
        The ChoCoTune instance from which music annotations will be extracted.

    Returns
    -------
    chords : list of lists
        A list of chord annotations as tuples: (measure, beat, duration, chord) 
    keys : list of lists
        A list of chord annotations as tuples: (measure, beat, duration, key)

    """
    measures = tune.measures_as_strings
    measure_beats = tune.time_signature[0]
    duration = measure_beats*len(measures)

    chords = []  # iterating and timing chords
    for m, measure in enumerate(measures):
        measure_chords = measure.split()
        chord_dur = measure_beats / len(measure_chords)
        # Creating equal onsets depending on within-measure chords and beats
        onsets = np.cumsum([0]+[d for d in (len(measure_chords)-1)*[chord_dur]])
        chords += [[m, o, chord_dur, c] for o, c in zip(onsets, measure_chords)]
    # Encapsulating key information as a single annotation
    assert len(tune.key.split()) == 1, "Single key assumed for iReal tunes"
    keys = [[0, 0, duration, tune.key]]

    return chords, keys


def jamify_ireal_tune(tune:ChoCoTune):
    """
    Create a JAMS from a given iReal tune, provided as ChoCoTune object, and
    a dictionary containing the metadata extracted from the tune.

    Parameters
    ----------
    tune : ChocoTune
        An instance of ChoCoTune, which will be jamified.
    
    Returns
    -------
    tune_meta : dict
        A dictionary containing the metadata of the tune.
    jam : jams.JAMS
        A JAMS object wrapping the tune annotations.

    """
    jam = jams.JAMS()
    tune_meta = extract_metadata_from_tune(tune)
    chords, keys = extract_annotations_from_tune(tune)

    append_metadata(jam, tune_meta)
    append_listed_annotation(jam, "chord", chords)
    append_listed_annotation(jam, "key_mode", keys)

    return tune_meta, jam


def process_ireal_charts(chart_data):
    """
    Read and process iReal chart data or tunes to create a JAMS dataset.

    Parameters
    ----------
    chart_data : list of ChoCoTune, or str
        Either a list containing instances of ChoCoTune created previously, or
        a string encoding all (raw) charts, or a path to a file containing the
        raw iReal charts.

    Returns
    -------
    metadata_list : list of dicts
        A list of dictionaries, each providing the metadata of a single tune.
    jams_list : list of jams.JAMS
        A list of JAMS files generated from the extraction process.

    """
    if isinstance(chart_data, str):
        if os.path.isfile(chart_data):
            with open(chart_data, 'r') as charts:
                chart_data = charts.read()
        if re.match(IREAL_RE, chart_data):
            tunes, _ = ChoCoTune.parse_ireal_url(chart_data)
    elif isinstance(chart_data, list) and \
        isinstance(chart_data[0], ChoCoTune):
        tunes = chart_data  # ready to go

    else:  # none of the supported parameter types/formats
        raise ValueError("Not a valid supported format or broken charts")

    
    jam_pack = [jamify_ireal_tune(tune) for tune in tunes]
    metadata_list, jam_list = list(zip(*jam_pack))

    return metadata_list, jam_list


def parse_ireal_dataset(dataset_dir, out_dir, dataset_name, **kwargs):
    """
    Process an iReal dataset to extract metadata information as well as JAMS
    annotations of chords and keys.

    Parameters
    ----------
    dataset_dir : str
        Path to an iReal dataset containing chart data in .txt files.
    out_dir : str
        Path to the output directory where JAMS annotations will be saved.
    dataset_name : str
        Name of the dataset that which will be used for the creation of new ids
        in both the metadata returned the JAMS files produced.

    Returns
    -------
    metadata_df : pandas.DataFrame
        A dataframe containing the retrieved and integrated content metadata.

    """
    offset_cnt = 0
    all_metadata = []

    jams_dir = create_dir(os.path.join(out_dir, "jams"))
    chart_files = glob.glob(os.path.join(dataset_dir, "*.txt"))
    logger.info(f"Found {len(chart_files)} .txt files for iReal parsing")

    for chart_file in chart_files:
        for i, (meta, jam) in enumerate(zip(*process_ireal_charts(chart_file))):

            meta["id"] = f"{dataset_name}_{offset_cnt + i}"
            meta["jams_path"] = None  # in case of error
            jams_path = os.path.join(jams_dir, meta["id"]+".jams")
            try:  # attempt saving the JAMS annotation file to disk
                jam.save(jams_path, strict=False)
                meta["jams_path"] = jams_path
            except:  # dumping error, logging for now
                logging.error(f"Could not save: {jams_path}")
            all_metadata.append(meta)
        offset_cnt = offset_cnt + i + 1
    # Finalise the metadata dataframe
    metadata_df = pd.DataFrame(all_metadata)
    metadata_df = metadata_df.set_index("id", drop=True)
    metadata_df.to_csv(os.path.join(out_dir, "meta.csv"))

    return metadata_df