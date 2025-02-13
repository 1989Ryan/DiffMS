from rdkit import Chem, RDLogger
from rdkit.Chem.rdchem import BondType as BT

import os
import pathlib
from typing import Any, Sequence

import torch
import torch.nn.functional as F
from tqdm import tqdm
import numpy as np
from torch_geometric.data import Data, InMemoryDataset
import pandas as pd
from rdkit.Chem.AllChem import GetMorganFingerprintAsBitVect

from src import utils
from src.analysis.rdkit_functions import mol2smiles, build_molecule_with_partial_charges, compute_molecular_metrics
from src.datasets.abstract_dataset import AbstractDatasetInfos, MolecularDataModule
from src.datasets.abstract_dataset import ATOM_TO_VALENCY, ATOM_TO_WEIGHT


def to_list(value: Any) -> Sequence:
    if isinstance(value, Sequence) and not isinstance(value, str):
        return value
    else:
        return [value]

atom_decoder = ['C', 'O', 'P', 'N', 'S', 'Cl', 'F', 'H']
valency = [ATOM_TO_VALENCY.get(atom, 0) for atom in atom_decoder]

# Data sources: 
# HMDB: https://hmdb.ca/downloads
# DSSTox: https://clowder.edap-cluster.com/datasets/61147fefe4b0856fdc65639b#folderId=6616d85ce4b063812d70fc8f
# COCONUT: https://zenodo.org/records/13692394

class FP2MolDataset(InMemoryDataset):
    def __init__(self, stage, root, filter_dataset: bool, transform=None, pre_transform=None, pre_filter=None, morgan_r=2, morgan_nBits=2048, dataset='hmdb'):
        self.stage = stage
        self.atom_decoder = atom_decoder
        self.filter_dataset = filter_dataset

        self.morgan_r = morgan_r
        self.morgan_nbits = morgan_nBits
        self.dataset = dataset

        self._processed_dir = os.path.join(root, 'processed', f'morgan_r-{self.morgan_r}__morgan_nbits-{self.morgan_nbits}')
        self._raw_dir = os.path.join(root, 'preprocessed')

        if self.stage == 'train': self.file_idx = 0
        elif self.stage == 'val': self.file_idx = 1
        elif self.stage == 'test': self.file_idx = 1
        else: raise ValueError(f"Invalid stage {self.stage}")

        super().__init__(root, None, pre_transform, pre_filter)
        self.data, self.slices = torch.load(self.processed_paths[self.file_idx])

    @property
    def processed_dir(self):
        return self._processed_dir

    @property
    def raw_file_names(self):
        if self.dataset == 'hmdb':
            return ['hmdb_train.csv', 'hmdb_val.csv']
        elif self.dataset == 'dss':
            return ['dss_train.csv', 'dss_val.csv']
        elif self.dataset == 'coconut':
            return ['coconut_train.csv', 'coconut_val.csv']
        elif self.dataset == 'combined':
            return ['combined_train.csv', 'combined_val.csv']
        elif self.dataset == 'moses':
            return ['moses_train.csv', 'moses_val.csv']
        elif self.dataset == 'combined_moses':
            return ['combined_moses_train.csv', 'combined_moses_val.csv']
        elif self.dataset == 'canopus':
            return ['canopus_train.csv', 'canopus_val.csv']
        elif self.dataset == 'smml':
            return ['smml_train.csv', 'smml_val.csv']
        elif self.dataset == 'combined_smml':
            return ['combined_smml_train.csv', 'combined_smml_val.csv']
        elif self.dataset == 'msg':
            return ['msg_train.csv', 'msg_test.csv']
        else:
            raise ValueError(f"Unkown Dataset {self.dataset}")

    @property
    def split_file_name(self):
        if self.dataset == 'hmdb':
            return ['hmdb_train.csv', 'hmdb_val.csv']
        elif self.dataset == 'dss':
            return ['dss_train.csv', 'dss_val.csv']
        elif self.dataset == 'coconut':
            return ['coconut_train.csv', 'coconut_val.csv']
        elif self.dataset == 'combined':
            return ['combined_train.csv', 'combined_val.csv']
        elif self.dataset == 'moses':
            return ['moses_train.csv', 'moses_val.csv']
        elif self.dataset == 'combined_moses':
            return ['combined_moses_train.csv', 'combined_moses_val.csv']
        elif self.dataset == 'canopus':
            return ['canopus_train.csv', 'canopus_val.csv']
        elif self.dataset == 'smml':
            return ['smml_train.csv', 'smml_val.csv']
        elif self.dataset == 'combined_smml':
            return ['combined_smml_train.csv', 'combined_smml_val.csv']
        elif self.dataset == 'msg':
            return ['msg_train.csv', 'msg_test.csv']
        else:
            raise ValueError(f"Unkown Dataset {self.dataset}")

    @property
    def split_paths(self):
        r"""The absolute filepaths that must be present in order to skip
        splitting."""
        files = to_list(self.split_file_name)
        return [os.path.join(self._raw_dir, f) for f in files]

    @property
    def processed_file_names(self):
        return ['train.pt', 'val.pt', 'test.pt']

    def process(self):
        RDLogger.DisableLog('rdApp.*')
        types = {atom: i for i, atom in enumerate(self.atom_decoder)}

        bonds = {BT.SINGLE: 0, BT.DOUBLE: 1, BT.TRIPLE: 2, BT.AROMATIC: 3}

        path = self.split_paths[self.file_idx]
        inchi_list = pd.read_csv(path)['inchi'].values

        if not os.path.exists(self.processed_paths[self.file_idx]):
            data_list = []
            smiles_kept = []

            for i, inchi in enumerate(tqdm(inchi_list)):
                try:
                    mol = Chem.MolFromInchi(inchi) 
                    smi = Chem.MolToSmiles(mol, isomericSmiles=False) # remove stereochemistry information
                    mol = Chem.MolFromSmiles(smi)

                    N = mol.GetNumAtoms()

                    type_idx = []
                    for atom in mol.GetAtoms():
                        type_idx.append(types[atom.GetSymbol()])

                    row, col, edge_type = [], [], []
                    for bond in mol.GetBonds():
                        start, end = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
                        row += [start, end]
                        col += [end, start]
                        edge_type += 2 * [bonds[bond.GetBondType()] + 1]

                    if len(row) == 0:
                        continue

                    edge_index = torch.tensor([row, col], dtype=torch.long)
                    edge_type = torch.tensor(edge_type, dtype=torch.long)
                    edge_attr = F.one_hot(edge_type, num_classes=len(bonds) + 1).to(torch.float)

                    perm = (edge_index[0] * N + edge_index[1]).argsort()
                    edge_index = edge_index[:, perm]
                    edge_attr = edge_attr[perm]

                    x = F.one_hot(torch.tensor(type_idx), num_classes=len(types)).float()
                    y = torch.tensor(np.asarray(GetMorganFingerprintAsBitVect(mol, self.morgan_r, nBits=self.morgan_nbits), dtype=np.int8)).unsqueeze(0)

                    inchi = Chem.MolToInchi(mol)

                    data = Data(x=x, edge_index=edge_index, edge_attr=edge_attr, y=y, idx=i, inchi=inchi)

                    if self.filter_dataset: # TODO: Check filter_dataset
                        # Try to build the molecule again from the graph. If it fails, do not add it to the training set
                        dense_data, node_mask = utils.to_dense(data.x, data.edge_index, data.edge_attr, data.batch)
                        dense_data = dense_data.mask(node_mask, collapse=True)
                        X, E = dense_data.X, dense_data.E

                        assert X.size(0) == 1
                        atom_types = X[0]
                        edge_types = E[0]
                        mol = build_molecule_with_partial_charges(atom_types, edge_types, atom_decoder)
                        smiles = mol2smiles(mol)
                        if smiles is not None:
                            try:
                                mol_frags = Chem.rdmolops.GetMolFrags(mol, asMols=True, sanitizeFrags=True)
                                if len(mol_frags) == 1:
                                    data_list.append(data)
                                    smiles_kept.append(smiles)

                            except Chem.rdchem.AtomValenceException:
                                print("Valence error in GetmolFrags")
                            except Chem.rdchem.KekulizeException:
                                print("Can't kekulize molecule")
                    else:
                        if self.pre_filter is not None and not self.pre_filter(data):
                            continue
                        if self.pre_transform is not None:
                            data = self.pre_transform(data)
                        data_list.append(data)
                except Exception as e:
                    print(e)

            torch.save(self.collate(data_list), self.processed_paths[self.file_idx])


class FP2MolDataModule(MolecularDataModule):
    def __init__(self, cfg):
        self.remove_h = False
        self.datadir = cfg.dataset.datadir
        self.filter_dataset = cfg.dataset.filter
        self.train_smiles = []
        self.dataset_name = cfg.dataset.dataset
        self._root_path = os.path.join(cfg.general.parent_dir, self.datadir, self.dataset_name)
        datasets = {'train': FP2MolDataset(stage='train', root=self._root_path, filter_dataset=self.filter_dataset, morgan_r=cfg.dataset.morgan_r, morgan_nBits=cfg.dataset.morgan_nbits, dataset=cfg.dataset.dataset),
                    'val': FP2MolDataset(stage='val', root=self._root_path, filter_dataset=self.filter_dataset, morgan_r=cfg.dataset.morgan_r, morgan_nBits=cfg.dataset.morgan_nbits, dataset=cfg.dataset.dataset),
                    'test': FP2MolDataset(stage='val', root=self._root_path, filter_dataset=self.filter_dataset, morgan_r=cfg.dataset.morgan_r, morgan_nBits=cfg.dataset.morgan_nbits, dataset=cfg.dataset.dataset)}
        super().__init__(cfg, datasets)


class FP2Mol_infos(AbstractDatasetInfos):
    def __init__(self, datamodule, cfg, recompute_statistics=False, meta=None):
        self.name = datamodule.dataset_name
        self.input_dims = None
        self.output_dims = None
        self.remove_h = False

        self.atom_decoder = atom_decoder
        self.atom_encoder = {atom: i for i, atom in enumerate(self.atom_decoder)}
        self.atom_weights = {i: ATOM_TO_WEIGHT.get(atom, 0) for i, atom in enumerate(self.atom_decoder)}
        self.valencies = valency
        self.num_atom_types = len(self.atom_decoder)
        self.max_weight = max(self.atom_weights.values())

        meta_files = dict(n_nodes=f'{datamodule._root_path}/stats/n_counts.txt',
                          node_types=f'{datamodule._root_path}/stats/atom_types.txt',
                          edge_types=f'{datamodule._root_path}/stats/edge_types.txt',
                          valency_distribution=f'{datamodule._root_path}/stats/valencies.txt')
        
        # n_nodes and valency_distribution are not transferrable between datatsets because of shape mismatches
        if cfg.dataset.stats_dir:
            meta_read = dict(n_nodes=f'{datamodule._root_path}/stats/n_counts.txt',
                          node_types=f'{cfg.dataset.stats_dir}/atom_types.txt',
                          edge_types=f'{cfg.dataset.stats_dir}/edge_types.txt',
                          valency_distribution=f'{datamodule._root_path}/stats/valencies.txt')
        else:
            meta_read = dict(n_nodes=f'{datamodule._root_path}/stats/n_counts.txt',
                          node_types=f'{datamodule._root_path}/stats/atom_types.txt',
                          edge_types=f'{datamodule._root_path}/stats/edge_types.txt',
                          valency_distribution=f'{datamodule._root_path}/stats/valencies.txt')
            

        self.n_nodes = None
        self.node_types = None
        self.edge_types = None
        self.valency_distribution = None

        if meta is None:
            meta = dict(n_nodes=None, node_types=None, edge_types=None, valency_distribution=None)
        assert set(meta.keys()) == set(meta_files.keys())

        for k, v in meta_read.items():
            if (k not in meta or meta[k] is None) and os.path.exists(v):
                meta[k] = torch.tensor(np.loadtxt(v))
                setattr(self, k, meta[k])

        self.max_n_nodes = len(self.n_nodes) - 1 if self.n_nodes is not None else None

        if recompute_statistics or self.n_nodes is None:
            self.n_nodes = datamodule.node_counts()
            print("Distribution of number of nodes", self.n_nodes)
            np.savetxt(meta_files["n_nodes"], self.n_nodes.numpy())
            self.max_n_nodes = len(self.n_nodes) - 1
        if recompute_statistics or self.node_types is None:
            self.node_types = datamodule.node_types()                                     # There are no node types
            print("Distribution of node types", self.node_types)
            np.savetxt(meta_files["node_types"], self.node_types.numpy())

        if recompute_statistics or self.edge_types is None:
            self.edge_types = datamodule.edge_counts()
            print("Distribution of edge types", self.edge_types)
            np.savetxt(meta_files["edge_types"], self.edge_types.numpy())
        if recompute_statistics or self.valency_distribution is None:
            valencies = datamodule.valency_count(self.max_n_nodes)
            print("Distribution of the valencies", valencies)
            np.savetxt(meta_files["valency_distribution"], valencies.numpy())
            self.valency_distribution = valencies

        self.complete_infos(n_nodes=self.n_nodes, node_types=self.node_types)