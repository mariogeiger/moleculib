import os
import traceback
from functools import partial
from pathlib import Path
from tempfile import gettempdir
from typing import List, Union

import biotite
import numpy as np
import pandas as pd
from pandas import DataFrame, Series
from torch.utils.data import Dataset
from tqdm.contrib.concurrent import process_map

from .alphabet import UNK_TOKEN
from .datum import ProteinDatum
from .transform import ProteinTransform
from .utils import pids_file_to_list

MAX_COMPLEX_SIZE = 32
PDB_METADATA_FIELDS = [
    ("idcode", str),
    ("num_res", int),
    ("standard", bool),
    ("resolution", float),
]
PDB_METADATA_FIELDS += [(f"num_res_{idx}", int) for idx in range(MAX_COMPLEX_SIZE)]


class ProteinDataset(Dataset):
    """
    Holds ProteinDatum dataset with specified PDB IDs

    Arguments:
    ----------
    base_path : str
        directory to store all PDB files
    pdb_ids : List[str]
        list of all protein IDs that should be in the dataset
    format : str
        the file format for each PDB file, either "npz" or "pdb"
    attrs : Union[str, List[str]]
        a partial list of protein attributes that should be in each protein
    """

    def __init__(
        self,
        base_path: str,
        transform: ProteinTransform = None,
        attrs: Union[List[str], str] = "all",
        metadata: pd.DataFrame = None,
        max_resolution: float = None,
        min_sequence_length: int = None,
        max_sequence_length: int = None,
    ):

        super().__init__()
        self.base_path = Path(base_path)
        if metadata is None:
            metadata = pd.read_hdf(str(self.base_path / "metadata.h5"))
        self.metadata = metadata
        self.transform = transform

        if max_resolution is not None:
            self.metadata = self.metadata[self.metadata["resolution"] <= max_resolution]

        if min_sequence_length is not None:
            self.metadata = self.metadata[
                self.metadata["num_res_0"] >= min_sequence_length
            ]

        if max_sequence_length is not None:
            self.metadata = self.metadata[
                self.metadata["num_res_0"] <= max_sequence_length
            ]

        # shuffle
        self.metadata = self.metadata.sample(frac=1).reset_index(drop=True)

        # specific protein attributes
        protein_attrs = [
            "idcode",
            "resolution",
            "residue_token",
            "residue_index",
            "residue_token",
            "residue_mask",
            "chain_token",
            "atom_token",
            "atom_coord",
            "atom_mask",
        ]

        if attrs == "all":
            self.attrs = protein_attrs
        else:
            for attr in attrs:
                if attr not in protein_attrs:
                    raise AttributeError(f"attribute {attr} is invalid")
            self.attrs = attrs

    def __len__(self):
        return len(self.metadata)

    def __getitem__(self, idx):
        pdb_id = self.metadata.iloc[idx]["idcode"]
        filepath = os.path.join(self.base_path, f"{pdb_id}.pdb")
        protein = ProteinDatum.from_filepath(filepath)
        if self.transform is not None:
            protein = self.transform.transform(protein)
        return protein

    @staticmethod
    def _extract_datum_row(datum):
        is_standard = not (datum.residue_token == UNK_TOKEN).all()
        metrics = dict(
            idcode=datum.idcode,
            standard=is_standard,
            resolution=datum.resolution,
            num_res=len(datum.sequence),
        )
        for chain in range(np.max(datum.chain_token)):
            num_residues = (datum.chain_token == chain).sum()
            metrics[f"num_res_{chain}"] = num_residues
        return Series(metrics).to_frame().T

    @staticmethod
    def _maybe_fetch_and_extract(pdb_id, save_path):
        try:
            datum = ProteinDatum.fetch_pdb_id(pdb_id, save_path=save_path)
        except KeyboardInterrupt:
            exit()
        except (ValueError, IndexError) as error:
            print(traceback.format_exc())
            print(error)
            return None
        except (biotite.database.RequestError) as request_error:
            print(request_error)
            return None
        if len(datum.sequence) == 0:
            return None
        return ProteinDataset._extract_datum_row(datum)

    @classmethod
    def build(
        cls,
        pdb_ids: List[str] = None,
        save: bool = True,
        save_path: str = None,
        max_workers: int = 1,
        **kwargs,
    ):
        """
        Builds dataset from scratch given specified pdb_ids, prepares
        data and metadata for later use.
        """
        if pdb_ids is None:
            root = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
            pdb_ids = pids_file_to_list(root + "/data/pids_all.txt")
        if save_path is None:
            save_path = gettempdir()

        series = {c: Series(dtype=t) for (c, t) in PDB_METADATA_FIELDS}
        metadata = DataFrame(series)

        extractor = partial(cls._maybe_fetch_and_extract, save_path=save_path)
        if max_workers > 1:
            rows = process_map(
                extractor, pdb_ids, max_workers=max_workers, chunksize=50
            )
        else:
            rows = list(map(extractor, pdb_ids))
        rows = filter(lambda row: row is not None, rows)

        metadata = pd.concat((metadata, *rows), axis=0)
        if save:
            metadata.to_hdf(str(Path(save_path) / "metadata.h5"), key="metadata")

        return cls(base_path=save_path, metadata=metadata, **kwargs)
