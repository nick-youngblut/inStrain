#!/usr/bin/env python3
"""
Self-contained script to generate a rarefaction curve
"""

__author__ = "Matt Olm"
__version__ = "0.1.0"
__license__ = "MIT"

import logging
import argparse
import copy
import os
import pandas as pd
import glob
import sys
import shutil

from collections import defaultdict
import subprocess
from subprocess import PIPE, run, Popen
from Bio import SeqIO

import inStrain
import inStrain.controller
import inStrain.profile
import inStrain.quickProfile
import inStrain.genomeUtilities

class Controller(object):
    def __init__(self, args):
        """
        Set all of the command line arguments in the "args" attribute
        Doing it this way lets your pass the arguments to other controllers
        """
        self.args = args
        self.ori_args = copy.deepcopy(args)

        # handle s3 aspects
        self.handle_s3_download()

        # Establish output directory and log
        OD = Outdir(self.args.output)
        self.OD = OD

    def main(self):
        """
        The main method when run from the command line
        """
        # parse arguments
        self.validate_arguments()

        # figure out the rarefication steps
        self.calc_rarefication_steps()

        # run the actual rarefaction steps
        self.run_rarefaction()

        # handle s3 aspects
        self.handle_s3_upload()

    def handle_s3_download(self):
        """
        Download s3 objects locally and reset the "args"
        """
        if self.args.s3_upload:
            from job_utils import generate_working_dir, delete_working_dir  # , setup_logger
            from s3_utils import download_file, upload_file, download_folder, upload_folder, read_s3_file

            tmp_dir = generate_working_dir('/mnt/temp')

            logging.info("Downloading to {0}".format(tmp_dir))

            download_file(self.args.bam, tmp_dir)
            self.args.bam = os.path.join(tmp_dir, os.path.basename(self.args.bam))

            download_file(self.args.fasta, tmp_dir)
            self.args.fasta = os.path.join(tmp_dir, os.path.basename(self.args.fasta))

            download_file(self.args.stb, tmp_dir)
            self.args.stb = os.path.join(tmp_dir, os.path.basename(self.args.stb))

            outdir = generate_working_dir('/mnt/scratch')
            self.args.output = os.path.join(outdir, os.path.basename(self.args.output))

    def handle_s3_upload(self):
        """
        Download s3 objects locally and reset the "args"
        """
        if self.args.s3_upload:
            from job_utils import generate_working_dir, delete_working_dir  # , setup_logger
            from s3_utils import download_file, upload_file, download_folder, upload_folder, read_s3_file

            upload_folder(self.args.s3_upload, self.args.output)


    def validate_arguments(self):
        """
        Do some parsing, set up a logger
        """
        # Make sure the bam file and stb file exist
        for f in [self.args.bam]:
            if not os.path.isfile(f):
                logging.error(f"{f} does not exist; crashing now")
                raise Exception(f"{f} does not exist")

        # Make the bam file if you need to; remove it from args
        self.bam = inStrain.profile.samtools_ops.prepare_bam_fie(self.args.bam, self.args.processes)
        del self.args.bam

        # Figure out number of reads in the .bam file
        self.total_reads = calc_number_reads(self.bam)
        logging.info(f"{self.total_reads:,} reads detected in bam file")

        # Figure out the average read length
        self.read_length = calc_read_length(self.bam)
        logging.info(f"Average read length in file is {self.read_length}")

        # Calculate genome2length
        self.genome2length, self.stb = calc_genome2length(self.args.fasta, self.args.stb)
        logging.info(f"Total Gbp in file is {(self.read_length * self.total_reads)/1e9:.2f}")

    def calc_rarefication_steps(self):
        """
        Figure out the .bam subsets you're going to do and give them all IDs. Store as a dataframe (?)
        """
        # Calculate mapping_factor to account for unmapped reads
        bam_reads = self.total_reads
        if self.args.total_reads != 0:
            total_reads = self.args.total_reads
            assert total_reads > bam_reads
        else:
            total_reads = bam_reads
        mapping_factor = bam_reads / total_reads

        # Set up iteration
        start_reads = self.args.start
        end_reads = self.args.end
        step = self.args.step

        # Re-do if Gb level
        if self.args.Gbp_level:
            if (self.args.start == 1000) & (self.args.end == 0) & (self.args.step == 1000000):
                start_reads = 1e9/self.read_length #(1Gbp)
                step = 1e9/self.read_length #(1Gbp)
                end_reads = total_reads
            else:
                start_reads = (1e9 * self.args.start) / self.read_length
                step = (1e9 * self.args.step)  / self.read_length
                end_reads = (1e9 * self.args.end) / self.read_length

        if end_reads == 0:
            end_reads = total_reads

        # Make table
        step_count = 1
        current_reads = start_reads
        table = defaultdict(list)
        while True:
            subset_reads = current_reads * mapping_factor

            if (subset_reads > end_reads) | (subset_reads > bam_reads):
                break

            overall_factor = subset_reads / total_reads
            for i in range(self.args.iterations):
                table['step_count'].append(step_count)
                table['total_reads_at_subset'].append(current_reads)
                table['total_Gbp_at_subset'].append((current_reads * self.read_length)/1e9)
                table['mapping_factor'].append(mapping_factor)
                table['subset_reads'].append(subset_reads)
                table['overall_factor'].append(overall_factor)
                table['seed'].append(i)
                table['samtools_subset'].append(str(i) + f"{overall_factor:.3f}"[1:])

            current_reads += step
            step_count += 1


        Tdb = pd.DataFrame(table)
        logging.info(f"Will create {len(Tdb)} different rarefication steps")
        self.rare_steps = Tdb
        self.OD.store('subset_table', Tdb)

    def run_rarefaction(self):
        """
        Run the actual subsets and stuff
        """
        bam_folder = self.OD.get('bam_folder')
        bam_base = os.path.join(bam_folder, os.path.basename(self.bam))

        dbs = []
        Tdb = self.rare_steps
        for i, row in Tdb.iterrows():
            # subset the bam
            new_bamloc = f"{bam_base}.subset.{row['samtools_subset']}.bam"
            sub_bam = subset_bam(self.bam, row['samtools_subset'], new_bamloc, p=self.args.processes)
            assert os.path.exists(sub_bam), sub_bam

            # run coverM
            qp_outloc = f"{bam_base}.subset.{row['samtools_subset']}.qp."
            Cdb = run_instrain_qp(sub_bam, qp_outloc, self.stb, self.genome2length, p=self.args.processes)
            Cdb['samtools_subset'] = row['samtools_subset']
            dbs.append(Cdb)

            # Clean up
            os.remove(sub_bam)

        Rdb = pd.concat(dbs).reset_index(drop=True)
        self.OD.store('rarefaction_table', Rdb)

class Namespace:
    """
    used for passing args to QP
    """
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

class Outdir:
    """
    A class to handle interfacing with saving and loading files to disk
    """

    def __init__(self, location):
        # Parse and store the location
        self.location = os.path.abspath(location)

        # Start a log file
        self.initialize()

    def initialize(self):
        """
        Create a directory and make a log
        """
        location = self.location

        if not os.path.exists(location):
            os.makedirs(location)

        for l in ['log', 'raw_data', 'output']:
            loc = location + '/' + l
            if not os.path.exists(loc):
                os.makedirs(loc)

        log_loc = os.path.join(location, 'log', 'log.log')
        inStrain.controller.setup_logger(log_loc)

    def store(self, name, obj):
        if name in ['subset_table', 'rarefaction_table']:
            loc = os.path.join(self.location, 'output', name + '.csv')
            obj.to_csv(loc, index=False)

    def get(self, name):
        if name == 'bam_folder':
            loc = os.path.join(self.location, 'raw_data', 'subset_bams')
            if not os.path.exists(loc):
                os.makedirs(loc)
            return loc

def calc_number_reads(bam):
    """
    Return the number of reads in the .bam file
    """
    cmd = ['samtools', 'idxstats', bam]
    logging.info(' '.join(cmd))

    # run idxstats
    result = run(cmd, stdout=PIPE, stderr=PIPE, universal_newlines=True)
    assert result.returncode == 0

    # sum number of reads
    total_reads = 0
    for line in result.stdout.split('\n'):
        if len(line.split()) != 4:
            continue
        total_reads += (int(line.split()[2]) + int(line.split()[3]))

    return total_reads

def calc_read_length(bam, num_reads=10000):
    """
    Return the average read length in the bam file
    """
    cmd = f"samtools view {bam} | head -n {num_reads}"
    result = run(cmd, stdout=PIPE, stderr=PIPE, universal_newlines=True, shell=True)
    assert result.returncode == 0

    # cal avg. read length
    total_reads = 0
    total_len = 0
    for line in result.stdout.split('\n'):
        if len(line.split()) <= 10:
            continue
        total_reads += 1
        total_len += len(line.split()[9])

    return total_len / total_reads

def subset_bam(ori_bam, substr, new_bam, p=6):
    cmd = f"samtools view -@ {p} -bs {substr} {ori_bam} > {new_bam}"
    result = run(cmd, stdout=PIPE, stderr=PIPE, universal_newlines=True, shell=True)
    assert result.returncode == 0
    return new_bam

def run_instrain_qp(sub_bam, out, stb, genome2length, p=6):
    # Get coverM exe
    loc = shutil.which('coverm')
    works = False
    if loc != None:
        try:
            o = subprocess.check_output([loc, '-h'], stderr=subprocess.STDOUT)
            works = True
        except:
            pass
    if not works:
        print("Cannot find coverm; make sure its installed")
        sys.exit()

    # Make args for the coverM command
    args = Namespace(bam=sub_bam, processes=p, stringent_breadth_cutoff=0, output=out)

    # Run the command
    Cdb = inStrain.quickProfile.run_coverm(loc, args)

    # Parse results
    args = Namespace(stb=stb)
    PCdb = inStrain.quickProfile.parse_coverm(Cdb, genome2length, args)
    return PCdb

def calc_genome2length(fasta, stb):
    # Get genome to length
    scaff2sequence = SeqIO.to_dict(SeqIO.parse(fasta, "fasta"))
    s2l = {s: len(scaff2sequence[s]) for s in list(scaff2sequence.keys())}
    del scaff2sequence

    # Set up the stb
    stb_loc = inStrain.genomeUtilities.load_scaff2bin([stb])

    if stb_loc == {}:
        stb_loc = {s: 'all_scaffolds' for s, l in s2l.items()}

    genome2length = {}
    for scaffold, length in s2l.items():
        if scaffold not in stb_loc:
            continue
        genome = stb_loc[scaffold]
        if genome not in genome2length:
            genome2length[genome] = 0
        genome2length[genome] += (
        s2l[scaffold])  # - 150) # 150 to account for the ignored ends; but it doesn't do that anymore

    return genome2length, stb_loc

if __name__ == "__main__":
    """ This is executed when run from the command line """
    parser = argparse.ArgumentParser()

    # Required arguments
    parser.add_argument("-b", "--bam", help="bam file")
    parser.add_argument("-f", "--fasta", help="fasta file")
    parser.add_argument("-s", "--stb", help="scaffold-to-bin file")
    parser.add_argument("-o", "--output", help="output folder location or name")
    parser.add_argument("-t", "--total_reads",
                        help="The total number of reads in the sample. If 0, will assume all reads are in the .bam file.",
                        type=int,
                        default=0)

    # Compute arguments
    parser.add_argument("-p", "--processes", help="number processes", type=int, default=6)

    # Rarefaction step arguments
    parser.add_argument("--start", help="the number of reads at the lowest step of rarefaction", type=int, default=1000)
    parser.add_argument("--end", help="the number of reads at the highest step of rarefaction. Set to 0 to step indefinately", type=int,
                        default=0)
    parser.add_argument("--step", help="the number of reads to step at each level", type=int,
                        default=1000000)
    parser.add_argument("--iterations", help="the number of times to repeat rarefaction", type=int,
                        default=10)
    parser.add_argument("--Gbp_level", help='Perform rarefaction based on Gbp instead of # reads. Will reset default steps to 1Gbp min with 1Gbp steps if defaults unchanged.',
                        default=False, action='store_true')

    # s3 stuff
    parser.add_argument("--s3_upload",
                        help='Upload results to s3, and download files from s3. Provide s3 location for upload',
                        default=False)



    args = parser.parse_args()
    Controller(args).main()