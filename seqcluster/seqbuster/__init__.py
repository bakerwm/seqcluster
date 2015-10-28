# Re-aligner small RNA sequence from SAM/BAM file (miRBase annotation)
import os.path as op
import shutil
import pandas as pd
import pysam

from bcbio import bam
from bcbio.provenance import do
from bcbio.utils import file_exists

import seqcluster.libs.logger as mylog
from seqcluster.align import pyMatch
from seqcluster.install import _get_miraligner
from realign import *

logger = mylog.getLogger(__name__)

def _download_mirbase(args, version="CURRENT"):
    """
    Download files from mirbase
    """
    if not args.hairpin or not args.mirna:
        logger.info("Working with version %s" % version)
        hairpin_fn = op.join(op.abspath(args.out), "hairpin.fa.gz")
        mirna_fn = op.join(op.abspath(args.out), "miRNA.str.gz")
        if not file_exists(hairpin_fn):
            cmd_h = "wget ftp://mirbase.org/pub/mirbase/%s/hairpin.fa.gz -O %s &&  gunzip -f !$" % (version, hairpin_fn)
            do.run(cmd_h, "download hairpin")
        if not file_exists(mirna_fn):
            cmd_m = "wget ftp://mirbase.org/pub/mirbase/%s/miRNA.str.gz -O %s && gunzip -f !$" % (version, mirna_fn)
            do.run(cmd_m, "download mirna")
    else:
        return args.hairpin, args.mirna

def _convert_to_fasta(fn):
    out_file = op.splitext(fn)[0] + ".fa"
    with open(out_file, 'w') as out_handle:
        with open(fn) as in_handle:
            for line in in_handle:
                if line.startswith("@"):
                    seq = in_handle.next()
                    _ = in_handle.next()
                    qual = in_handle.next()
                elif line.startswith(">"):
                    seq = in_handle.next()
                count = 2
                if line.find("_x"):
                    count = int(line.strip().split("_x")[1])
                if count > 1:
                    print >>out_handle, ">%s" % line.strip()[1:]
                    print >>out_handle, seq.strip()
    return out_file

def _get_pos(string):
    name = string.split(":")[0][1:]
    pos = string.split(":")[1][:-1].split("-")
    return name, pos

def _read_mature(matures, sps):
    mature = defaultdict(dict)
    with open(matures) as in_handle:
        for line in in_handle:
            if line.startswith(">") and line.find(sps) > -1:
                name = line.strip().replace(">", " ").split()
                mir5p = _get_pos(name[2])
                mature[name[0]] = {mir5p[0]: map(int, mir5p[1])}
                if len(name) > 3:
                    mir3p = _get_pos(name[3])
                    mature[name[0]].update({mir3p[0]: map(int, mir3p[1])})
    return mature

def _read_precursor(precursor, sps):
    """
    read precurso file for that species
    """
    hairpin = defaultdict(str)
    name = None
    with open(precursor) as in_handle:
        for line in in_handle:
            if line.startswith(">"):
                if hairpin[name]:
                    hairpin[name] = hairpin[name] + "NNNNNNNNNNNN"
                name = line.strip().replace(">", " ").split()[0]
            else:
                hairpin[name] += line.strip()
        hairpin[name] = hairpin[name] + "NNNNNNNNNNNN"
    return hairpin

def _coord(sequence, start, mirna, precursor, iso):
    """
    Define t5 and t3 isomirs
    """
    dif = abs(mirna[0] - start)
    if start < mirna[0]:
        iso.t5 = sequence[:dif].upper()
    elif start > mirna[0]:
        iso.t5 = precursor[mirna[0] - 1:mirna[0] - 1 + dif].lower()
    elif start == mirna[0]:
        iso.t5 = "NA"
    if dif > 4:
        logger.debug("start > 3 %s %s %s %s %s" % (start, len(sequence), dif, mirna, iso.format()))
        return None

    end = start + (len(sequence) - len(iso.add)) - 1
    dif = abs(mirna[1] - end)
    if iso.add:
        sequence = sequence[:-len(iso.add)]
    # if dif > 3:
    #    return None
    if end > mirna[1]:
        iso.t3 = sequence[-dif:].upper()
    elif end < mirna[1]:
        iso.t3 = precursor[mirna[1] - dif:mirna[1]].lower()
    elif end == mirna[1]:
        iso.t3 = "NA"
    if dif > 4:
        logger.debug("end > 3 %s %s %s %s %s" % (len(sequence), end, dif, mirna, iso.format()))
        return None
    logger.debug("%s %s %s %s %s %s" % (start, len(sequence), end, dif, mirna, iso.format()))
    return True

def _annotate(reads, mirbase_ref, precursors):
    """
    Using SAM/BAM coordinates, mismatches and realign to annotate isomiRs
    """
    for r in reads:
        for p in reads[r].precursors:
            start = reads[r].precursors[p].start + 1  # convert to 1base
            end = start + len(reads[r].sequence)
            for mature in mirbase_ref[p]:
                mi = mirbase_ref[p][mature]
                is_iso = _coord(reads[r].sequence, start, mi, precursors[p], reads[r].precursors[p])
                logger.debug(("{r} {p} {start} {is_iso} {mature} {mi} {mature_s}").format(s=reads[r].sequence, mature_s=precursors[p][mi[0]-1:mi[1]], **locals()))
                if is_iso:
                    reads[r].precursors[p].mirna = mature
                    break
    return reads

def _realign(seq, precursor, start):
    """
    The actual fn that will realign the sequence
    """
    error = set()
    pattern_addition = [[1, 1, 0], [1, 0, 1], [0, 1, 0], [0, 1, 1], [0, 0, 1], [1, 1, 1]]
    for pos in range(0, len(seq)):
        if seq[pos] != precursor[(start + pos)]:
            error.add(pos)

    subs, add = [], []
    for e in error:
        if e < len(seq) - 3:
            subs.append([e, seq[e]])

    pattern, error_add = [], []
    for e in range(len(seq) - 3, len(seq)):
        if e in error:
            pattern.append(1)
            error_add.append(e)
        else:
            pattern.append(0)
    for p in pattern_addition:
        if pattern == p:
            add = seq[error_add[0]:]
            break
    if not add and error_add:
        for e in error_add:
            subs.append([e, seq[e]])

    return subs, add

def _clean_hits(reads):
    """
    Select only best matches
    """
    new_reads = defaultdict(realign)
    for r in reads:
        world = {}
        sc = 0
        for p in reads[r].precursors:
            world[p] = reads[r].precursors[p].get_score(len(reads[r].sequence))
            if sc < world[p]:
                sc = world[p]
        new_reads[r] = reads[r]
        for p in world:
            logger.debug("score %s %s %s" % (r, p, world[p]))
            if sc != world[p]:
                logger.debug("remove %s %s %s" % (r, p, world[p]))
                new_reads[r].remove_precursor(p)

    return new_reads

def _sort_by_name(bam_fn):
    """
    sort bam file by name sequence
    """

def _read_bam(bam_fn, precursors):
    """
    read bam file and perform realignment of hits
    """
    handle = bam.open_samfile(bam_fn)
    reads = defaultdict(realign)
    for line in handle:
        chrom = handle.getrname(line.reference_id)
        # print "%s %s %s %s" % (line.query_name, line.reference_start, line.query_sequence, chrom)
        if line.query_name not in reads:
            reads[line.query_name].sequence = line.query_sequence
        iso = isomir()
        iso.align = line
        iso.start = line.reference_start
        iso.subs, iso.add = _realign(reads[line.query_name].sequence, precursors[chrom], line.reference_start)
        reads[line.query_name].set_precursor(chrom, iso)

    reads = _clean_hits(reads)
    return reads

def _read_pyMatch(fn, precursors):
    """
    read pyMatch file and perform realignment of hits
    """
    with open(fn) as handle:
        reads = defaultdict(realign)
        for line in handle:
            query_name, seq, chrom, reference_start, end, mism, add = line.split()
            reference_start = int(reference_start)
            # chrom = handle.getrname(cols[1])
            # print "%s %s %s %s" % (line.query_name, line.reference_start, line.query_sequence, chrom)
            if query_name not in reads:
                reads[query_name].sequence = seq
            iso = isomir()
            iso.align = line
            iso.start = reference_start
            iso.subs, iso.add = _realign(reads[query_name].sequence, precursors[chrom], reference_start)
            logger.debug("%s %s %s %s %s" % (query_name, reference_start, chrom, iso.subs, iso.add))
            reads[query_name].set_precursor(chrom, iso)

        reads = _clean_hits(reads)
    return reads

def _parse_mut(subs):
    """
    Parse mutation tag from miraligner output
    """
    if subs!="0":
        subs = [subs.replace(subs[-2:], ""),subs[-2], subs[-1]]
    return subs

def _read_miraligner(fn):
    """Read ouput of miraligner and create compatible output."""
    reads = defaultdict(realign)
    with open(fn) as in_handle:
        in_handle.next()
        for line in in_handle:
            cols = line.strip().split("\t")
            iso = isomir()
            query_name, seq = cols[1], cols[0]
            chrom, reference_start = cols[-2], cols[3]
            iso.mirna = cols[3]
            subs, add, iso.t5, iso.t3 = cols[6:10]
            if query_name not in reads:
                reads[query_name].sequence = seq
            iso.align = line
            iso.start = reference_start
            iso.subs, iso.add = _parse_mut(subs), add
            logger.debug("%s %s %s %s %s" % (query_name, reference_start, chrom, iso.subs, iso.add))
            reads[query_name].set_precursor(chrom, iso)
    return reads

def _cmd_miraligner(fn, out_file, species, hairpin):
    """
    Run miraligner for miRNA annotation
    """
    tool = _get_miraligner()
    path_db = op.dirname(op.abspath(hairpin))
    opts = "-Xms750m -Xmx4g"
    cmd = "{tool} -i {fn} -o {out_file} -s {species} -db {path_db} -sub 1 -trim 3 -add 3"
    if not file_exists(out_file):
        do.run(cmd.format(**locals()), "miraligner with %s" % fn)
        shutil.move(out_file + ".mirna", out_file)
    return out_file

def _get_freq(name):
    """
    Check if name read contains counts (_xNumber)
    """
    try:
        counts = name.split("_x")[1]
    except:
        return 0
    return counts

def _tab_output(reads, out_file, sample):
    seen = set()
    lines = []
    seen_ann = {}
    with open(out_file, 'w') as out_handle:
        print >>out_handle, "name\tseq\tfreq\tchrom\tstart\tend\tsubs\tadd\tt5\tt3\ts5\ts3\tDB\tprecursor\thits"
        for r, read in reads.iteritems():
            hits = set()
            [hits.add(mature.mirna) for mature in read.precursors.values() if mature.mirna]
            hits = len(hits)
            for p, iso in read.precursors.iteritems():
                if len(iso.subs) > 3 or not iso.mirna:
                    continue
                if (r, iso.mirna) not in seen:
                    seen.add((r, iso.mirna))
                    chrom = iso.mirna
                    if not chrom:
                        chrom = p
                    count = _get_freq(r)
                    seq = reads[r].sequence

                    annotation = "%s:%s" % (chrom, iso.format(":"))
                    res = ("{seq}\t{r}\t{count}\t{chrom}\tNA\tNA\t{format}\tNA\tNA\tmiRNA\t{p}\t{hits}").format(format=iso.format().replace("NA", "0"), **locals())
                    if annotation in seen_ann:
                        raise ValueError("Same isomir %s from different sequence: \n%s and \n%s" % (annotation, res, seen_ann[annotation]))
                    seen_ann[annotation] = res
                    lines.append([annotation, chrom, count, sample, hits])
                    print >>out_handle, res

    dt = pd.DataFrame(lines)
    dt.columns = ["isomir", "chrom", "counts", "sample", "hits"]
    dt.to_csv(out_file + "_summary")
    return out_file, dt

def _merge(dts):
    """
    merge multiple samples in one matrix
    """
    df= pd.concat(dts)

    df = df[df['hits']>0]
    ma = df.pivot(index='isomir', columns='sample', values='counts')
    ma_mirna = ma
    ma_mirna['mirna'] = [m.split(":")[0] for m in ma.index.values]
    ma_mirna = ma_mirna.groupby(['mirna']).sum()

    return ma, ma_mirna

def _create_counts(out_dts, out_dir):
    """Summarize results into single files."""
    ma, ma_mirna = _merge(out_dts)
    out_ma = op.join(out_dir, "counts.tsv")
    out_ma_mirna = op.join(out_dir, "counts_mirna.tsv")
    ma.to_csv(out_ma, sep="\t")
    ma_mirna.to_csv(out_ma_mirna, sep="\t")
    return out_ma_mirna, out_ma

def miraligner(args):
    """
    Realign BAM hits to miRBAse to get better accuracy and annotation
    """
    config = {"algorithm": {"num_cores": 1}}
    hairpin, mirna = _download_mirbase(args)
    precursors = _read_precursor(args.hairpin, args.sps)
    matures = _read_mature(args.mirna, args.sps)
    out_dts = []
    for bam_fn in args.files:
        sample = op.splitext(op.basename(bam_fn))[0]
        if bam_fn.endswith("bam") or bam_fn.endswith("sam"):
            logger.info("Reading %s" % bam_fn)
            bam_fn = bam.sam_to_bam(bam_fn, config)
            bam_sort_by_n = op.splitext(bam_fn)[0] + "_sort"
            pysam.sort("-n", bam_fn, bam_sort_by_n)
            reads = _read_bam(bam_sort_by_n + ".bam", precursors)
        elif bam_fn.endswith("fasta") or bam_fn.endswith("fa") or bam_fn.endswith("fastq"):
            out_file = op.join(args.out, sample + ".premirna")
            if args.miraligner:
                _cmd_miraligner(bam_fn, out_file, args.sps, args.hairpin)
                reads = _read_miraligner(out_file)
            else:
                if bam_fn.endswith("fastq"):
                    bam_fn = _convert_to_fasta(bam_fn)
                logger.info("Aligning %s" % bam_fn)
                if not file_exists(out_file):
                    pyMatch.Miraligner(hairpin, bam_fn, out_file, 1, 4)
                reads = _read_pyMatch(out_file, precursors)
        else:
            raise ValueError("Format not recognized.")

        if not args.miraligner:
            reads = _annotate(reads, matures, precursors)
        out_file = op.join(args.out, sample + ".mirna")
        out_file, dt = _tab_output(reads, out_file, sample)
        out_dts.append(dt)

    if out_dts:
        _create_counts(out_dts, args.out)
        # _summarize(out_dts)
