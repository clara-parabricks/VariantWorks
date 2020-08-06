#
# Copyright 2020 NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Classes and functions for encoding samples."""

import abc
from enum import Enum
import numpy as np
import pandas as pd
import pysam
import torch

from variantworks.base_encoder import base_enum_encoder
from variantworks.types import Variant, VariantType, VariantZygosity


class SampleEncoder:
    """An abstract class defining the interface to an encoder implementation.

    Encoder could be used for encoding inputs to network, as well as encoding target labels for prediction.
    """

    def __init__(self):
        """Construct a class instance."""
        pass

    @abc.abstractmethod
    def __call__(self, *sample):
        """Compute the encoding of a sample."""
        raise NotImplementedError


class SummaryEncoder(SampleEncoder):
    """A summary count encoder for pileups.

    For a given pileup of reads (e.g. output from samtools mpileup), the encoder generates
    tensor for each pileup column. The encoder counts the number of DNA bases (A, G, G, T, deletion)
    for each pileup column on both the forward and reverse strands. Insertions are handled by encoding
    new pileup columns. Therefore, the output of the encoder is a tensor of shape (num_pileup_col, 10).
    The output of this encoder can be used to train a sequence aware model such such as an RNN.
    """

    def __init__(self, exclude_no_coverage_positions=True, normalize_counts=True):
        """Constructor for the class.

        Args:
            exclude_no_coverage_positions : Flag to determine if pileup columns with 0
                                            coverage should be dropped.
            normalize_counts : Flag to determine if summary counts in encoding should
                               be normalized.

        Returns:
            Instance of class.
        """
        self._exclude_no_coverage_positions = exclude_no_coverage_positions
        self._normalize_counts = normalize_counts

        # Supported alphabet when building summary encoder.
        self.symbols = ["a",
                        "c",
                        "g",
                        "t",
                        "A",
                        "C",
                        "G",
                        "T",
                        "#",
                        "*"]

    def _find_insertions(self, base_pileup):
        """Finds all of the insertions in a given base's pileup string.

        Args:
            base_pileup: Single base's pileup string output from samtools mpileup

        Returns:
            insertions: list of all insertions in pileup string
            next_to_del: whether insertion is next to deletion symbol (should be ignored)

        """
        insertions = []
        idx = 0
        next_to_del = []
        while (idx < len(base_pileup)):
            if (base_pileup[idx] == "+"):
                end_of_number = False
                start_index = idx+1
                while not end_of_number:
                    if (base_pileup[start_index].isdigit()):
                        start_index += 1
                    else:
                        end_of_number = True
                insertion_length = int(base_pileup[idx:start_index])
                inserted_bases = base_pileup[idx+(start_index-idx):idx+(start_index-idx)+insertion_length]
                insertions.append(inserted_bases)
                if (base_pileup[idx-1] == "*" or base_pileup[idx-1] == "#"):
                    next_to_del.append(True)
                else:
                    next_to_del.append(False)
                idx += (start_index-idx) + 1 + insertion_length
            else:
                idx += 1
        return insertions, next_to_del

    def __call__(self, region):
        """Generate a torch tensor with summary encoding.

        Args:
            region : Region dataclass specifying region within a pileup to generate
                     an encoding for.
        """
        start_pos = region.start_pos
        end_pos = region.end_pos
        pileup_file = region.pileup

        # Load pileup file into a dataframe
        pileup = pd.read_csv(pileup_file, delimiter="\t", header=None).values

        if (len(pileup) < end_pos):
            end_pos = len(pileup)

        subreads = pileup[:, 4]
        truth_coverage = pileup[:, 7].astype("int")
        positions = []
        positions_insertions = []

        # Calculate major and minor positions
        for i in range(start_pos, end_pos):
            if self._exclude_no_coverage_positions and truth_coverage[i] == 0:
                continue

            base_pileup = subreads[i].strip("^]").strip("$")

            # Get all insertions in pileup
            insertions, next_to_del = self._find_insertions(base_pileup)

            # Find length of maximum insertion
            longest_insertion = len(max(insertions, key=len)) if insertions else 0

            # Keep track of major and minor positions in the pileup and the insertions
            # in the pileup.
            major_minor_pos = []  # Major position for ref base pos in pileup, minor for additional inserted bases
            major_minor_pos.append((i, 0))
            insertions_store = []
            insertions_store.append([])
            for j in range(longest_insertion):
                major_minor_pos.append((i, j+1))
                insertions_store.append(insertions)
            positions += major_minor_pos
            positions_insertions += (insertions_store)

        # Using positions, calculate pileup counts
        pileup_counts = np.zeros((len(positions), 10))
        for i in range(len(positions)):
            major_position = positions[i][0]
            minor_position = positions[i][1]
            base_pileup = subreads[major_position].strip("^]").strip("$")
            insertions, next_to_del = self._find_insertions(base_pileup)
            insertions_to_keep = []

            # Remove all insertions which are next to delete positions in pileup
            for k in range(len(insertions)):
                if next_to_del[k] is False:
                    insertions_to_keep.append(insertions[k])

            # Replace all occurrences of insertions from the pileup string
            for insertion in insertions:
                base_pileup = base_pileup.replace("+" + str(len(insertion)) + insertion, "")

            if (minor_position == 0):  # No insertions for this position
                for j in range(len(self.symbols)):
                    pileup_counts[i, j] = base_pileup.count(self.symbols[j])
            elif (minor_position > 0):
                # Remove all insertions which are smaller than minor position being considered
                # so we only count inserted bases at positions longer than the minor position
                insertions_minor = [x for x in insertions_to_keep if len(x) >= minor_position]
                for j in range(len(insertions_minor)):
                    inserted_base = insertions_minor[j][minor_position-1]
                    pileup_counts[i, self.symbols.index(inserted_base)] += 1

        positions = np.array(positions, dtype=[('major', '<i8'), ('minor', '<i8')])

        if self._normalize_counts:
            # Fetch all tensor positions wherebases were inserted
            minor_inds = np.where(positions['minor'] > 0)
            # Find corresponding reference base positions in tensor
            major_pos_at_minor_inds = positions['major'][minor_inds]
            # Find the index of major positions for the reference base positions
            major_ind_at_minor_inds = np.searchsorted(positions['major'], major_pos_at_minor_inds, side='left')
            # Calculate depth across all pileup columns
            depth = np.sum(pileup_counts, axis=1)
            # Replace the depth of minor columns with the depths of major columns for those minor columns
            depth[minor_inds] = depth[major_ind_at_minor_inds]
            # Normalize each column
            feature_array = pileup_counts / np.maximum(1, depth).reshape((-1, 1))
            return feature_array, positions
        else:
            return pileup_counts, positions


class PileupEncoder(SampleEncoder):
    """A read pileup encoder for BAMs.

    For a given SNP position and nucleotide context, the encoder generates a pileup
    tensor around the variant position. The pileup can have configurable depth based on
    the type of information that is selected to be embedded.
    The variant location of interest is kept centered in the pileup, and the layers input in
    the constructor define the channels created in the encoding. For more details on available
    channels, please check the documentation for the Layers enum.
    """

    class Layer(Enum):
        r"""Layers that can be added to the pileup encoding.

        Values:
            READ : Encode each aligned read as a row of the pileup. The bases in the
            read are encoded using a base_encoder dict passed into the class. The reads
            in the row are positioned according to the pileup alignment.
            BASE_QUALITY : Encode the base quality of each aligned read in the pileup. Base
            qualities of each read are added to a new row, following the same positioning as for READS. The base
            qualities are normalized to [0,1] (using max value of 93 per SAM format).
            Missing base quality is set to 0.
            MAPPING_QUALITY : Mapping quality of a read is encoded at each nucleotide position of the read. Mapping
            quality values are noramlize to [0,1] (assuming max value of 50).
            Missing mapping quality is set to 0.
            REFERENCE : Only the reference allele location is encoded in each row.
            ALLELE : Only the alt allele location is encoded in each row.
        """

        READ = 0
        BASE_QUALITY = 1
        MAPPING_QUALITY = 2
        REFERENCE = 3
        ALLELE = 4

    def __init__(self, window_size=50, max_reads=50, layers=[Layer.READ], base_encoder=None):
        """Construct class instance.

        Args:
            window_size : A nucleotide context size on either side of variant position [50].
            max_reads : Max number of reads to consider in the pileip. If reads fewer than max_reads
            are available, the entries are all masked to 0. [50]
            layers : A list defining the layers to add to the encoding. The ordering of channels in the
            encoding follows the ordering of layers in the list. [Layer.READ]
            base_encoder : A dict defining conversion of nucleotide string chars to numeric representation.
            [base_encoder.base_enum_encoder]

        Returns:
            Instance of class.
        """
        super().__init__()
        self.window_size = window_size
        self.max_reads = max_reads
        self.layers = layers
        self.bams = dict()
        self.base_encoder = base_encoder if base_encoder is not None else base_enum_encoder
        self.layer_tensors = []
        self.layer_dict = {}
        for layer in layers:
            tensor = torch.zeros(
                (self.height, self.width), dtype=torch.float32)
            self.layer_tensors.append(tensor)
            self.layer_dict[layer] = tensor

    @property
    def width(self):
        """Return width of pileup."""
        return 2 * self.window_size + 1

    @property
    def height(self):
        """Return height of pileup."""
        return self.max_reads

    @property
    def depth(self):
        """Return number of layers in pileup."""
        return len(self.layers)

    def _fill_layer(self, layer, pileupread, left_offset, right_offset, row, pileup_pos_range, variant):
        # print(len(pileupread.alignment.get_reference_sequence()))
        tensor = self.layer_dict[layer]

        query_pos = pileupread.query_position

        # Currently only support adding reads
        if layer == self.Layer.READ:
            # Fetch the subsequence based on the offsets
            seq = pileupread.alignment.query_sequence[query_pos -
                                                      left_offset: query_pos + right_offset]
            for seq_pos, pileup_pos in enumerate(range(pileup_pos_range[0], pileup_pos_range[1])):
                # Encode base characters to enum
                tensor[row, pileup_pos] = self.base_encoder[seq[seq_pos]]
        elif layer == self.Layer.BASE_QUALITY:
            # From SAM format docs.
            MAX_BASE_QUALITY = 93.0
            # Fetch the subsequence based on the offsets
            seq_qual = pileupread.alignment.query_qualities[query_pos -
                                                            left_offset: query_pos + right_offset]
            for seq_pos, pileup_pos in enumerate(range(pileup_pos_range[0], pileup_pos_range[1])):
                # Encode base characters to enum
                qual = seq_qual[seq_pos]
                if qual == 255:
                    qual = 0.
                else:
                    qual = qual / MAX_BASE_QUALITY
                tensor[row, pileup_pos] = qual
        elif layer == self.Layer.MAPPING_QUALITY:
            MAX_MAPPING_QUALITY = 50.0
            # Getch mapping quality of alignment
            map_qual = pileupread.alignment.mapping_quality
            # Missing mapiping quality is 255
            if map_qual == 255:
                map_qual = 0.0
            else:
                map_qual = pileupread.alignment.mapping_quality / MAX_MAPPING_QUALITY
            for pileup_pos in range(pileup_pos_range[0], pileup_pos_range[1]):
                # Encode base characters to enum
                tensor[row, pileup_pos] = map_qual
        elif layer == self.Layer.REFERENCE:
            # Only encode the reference at the variant position, rest all 0
            tensor[row, self.window_size] = self.base_encoder[variant.ref]
        elif layer == self.Layer.ALLELE:
            # Only encode the allele at the variant position, rest all 0
            tensor[row, self.window_size] = self.base_encoder[variant.allele]

    def __call__(self, variant):
        """Return a torch Tensor pileup queried from a BAM file.

        Args:
            variant : Variant struct holding information about variant locus.
        """
        # Locus information
        chrom = variant.chrom
        variant_pos = variant.pos
        bam_file = variant.bam

        assert(variant.type ==
               VariantType.SNP), "Only SNP variants supported in PileupEncoder currently."

        # Create BAM object if one hasn't been opened before.
        if bam_file not in self.bams:
            self.bams[bam_file] = pysam.AlignmentFile(bam_file, "rb")

        bam = self.bams[bam_file]

        # Get pileups from BAM.
        # Note that VCF positions are 1 based, but pysam pileup regions are 0 based.
        # So subtract one from position.
        pileups = bam.pileup(chrom,
                             variant_pos - 1, variant_pos,
                             truncate=True,
                             max_depth=self.max_reads)

        for col, pileup_col in enumerate(pileups):
            for row, pileupread in enumerate(pileup_col.pileups):
                # Skip rows beyond the max depth
                if row >= self.max_reads:
                    break
                # Check if reference base is missing (either deleted or skipped).
                if pileupread.is_del or pileupread.is_refskip:
                    continue

                # Using the variant locus as the center, find the left and right offset
                # from that locus to use as bounds for fetching bases from reads.
                #
                #      |------V------|
                #  ATCGATCGATCGATCG
                #        ATCGATCGATCGATCGATCG
                #
                # 1st read - Left offset is window size, and right offset is 4 bases
                # 2nd read - Left offset is 5 bases, and right offset is window size
                left_offset = min(self.window_size, pileupread.query_position)
                right_offset = min(self.window_size + 1, len(pileupread.alignment.query_sequence) -
                                   pileupread.query_position)

                pileup_pos_range = (
                    self.window_size - left_offset, self.window_size + right_offset)
                for layer in self.layers:
                    self._fill_layer(layer, pileupread, left_offset,
                                     right_offset, row, pileup_pos_range, variant)

        encoding = torch.stack(self.layer_tensors)
        [tensor.zero_() for tensor in self.layer_tensors]
        return encoding


class ZygosityLabelEncoder(SampleEncoder):
    """A label encoder that returns an output label encoding for zygosity only.

    Converts zygosity type to a class number.
    """

    def __init__(self):
        """Construct a class instance."""
        super().__init__()
        self._dict = {
            VariantZygosity.NO_VARIANT: 0,
            VariantZygosity.HOMOZYGOUS: 1,
            VariantZygosity.HETEROZYGOUS: 2,
        }

    def __call__(self, variant):
        """Encode variant to class for zygosity.

        Returns:
           Zygosity encoded as number.
        """
        assert(isinstance(variant, Variant))
        var_zyg = variant.zygosity
        assert(var_zyg in self._dict)

        return torch.tensor(self._dict[var_zyg])


class ZygosityLabelDecoder(SampleEncoder):
    """A decoder to convert a class to a zygosity enum."""

    def __init__(self):
        """Construct a class instance."""
        super().__init__()
        self._dict = {
            0: VariantZygosity.NO_VARIANT,
            1: VariantZygosity.HOMOZYGOUS,
            2: VariantZygosity.HETEROZYGOUS,
        }

    def __call__(self, class_id):
        """Decode class to variant zygosity enum.

        Returns:
            Variant zygosity.
        """
        assert(class_id.item() in self._dict)
        return self._dict[class_id.item()]
