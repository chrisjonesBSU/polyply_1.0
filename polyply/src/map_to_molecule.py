# Copyright 2020 University of Groningen
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import networkx as nx
from polyply.src.processor import Processor

def tag_exclusions(node_to_block, force_field):
    """
    Given the names of some `blocks` check if the
    corresponding molecules in `force_field` have
    the same number of default exclusions. If not
    find the minimum number of exclusions and tag all
    nodes with the original exclusion number. Then
    change the exclusion number.

    Note the tag is picked up by apply links where
    the excluions are generated.
    """
    excls = {}
    for node in node_to_block:
        block = force_field.blocks[node_to_block[node]]
        excls[node] = block.nrexcl

    if len(set(excls.values())) > 1:
        min_excl = min(list(excls.values()))
        for node, excl in excls.items():
            block = force_field.blocks[node_to_block[node]]
            nx.set_node_attributes(block, excl, "exclude")
            block.nrexcl = min_excl

def _correspondence_to_residue(meta_molecule,
                               molecule,
                               correspondence,
                               res_node):
    """
    Given a `meta_molecule` and the underlying higher resolution
    `molecule` as well as a correspondence dict, describing how
    a single node (res_node) in meta_molecule corresponds to a
    fragment in molecule make a graph of that residue and propagate
    all meta_molecule node attributes to that graph.

    Parameters
    ----------
    meta_molecule: polyply.src.meta_molecule.MetaMolecule
        The meta molecule to process.
    molecule: vermouth.molecule.Molecule
    correspondance: list
    res_node: abc.hashable
        the node in meta_molecule
    """
    resid = meta_molecule.nodes[res_node]["resid"]
    residue = nx.Graph()
    for mol_node in correspondence.values():
        data = molecule.nodes[mol_node]
        if data["resid"] == resid:
            residue.add_node(mol_node, **data)
            for attribute, value in meta_molecule.nodes[res_node].items():
                # graph and seqID are specific attributes set by make residue
                # graph or the gen_seq tool, which we don't want to propagate.
                if attribute in ["graph", "seqID"]:
                    continue
                residue.nodes[mol_node][attribute] = value

    return residue

class MapToMolecule(Processor):
    """
    This processor takes a :class:`MetaMolecule` and generates a
    :class:`vermouth.molecule.Molecule`, which consists at this stage
    of disconnected blocks. These blocks can be connected using the
    :class:`ApplyLinks` processor.
    """
    def __init__(self, force_field):
        self.node_to_block = {}
        self.node_to_fragment = {}
        self.fragments = []
        self.multiblock_correspondence = []
        self.added_fragments = []
        self.added_fragment_nodes = []
        self.force_field = force_field

    def match_nodes_to_blocks(self, meta_molecule):
        """
        This function matches the nodes in the meta_molecule
        to the blocks in the force-field. It does the essential
        bookkeeping for three cases and populates the node_to_block,
        and node_to_fragment dicts as well as the fragments attribute.
        It distinguishes three cases:

        1) the node corresponds to a single residue block; here
           node_to_block entry is simply the resname of the block
        2) the node has the from_itp attribute; in this case
           the node is part of a multiresidue block in the FF,
           all nodes corresponding to that block form a fragment.
           All fragments are added to the fragments attribute, and
           the nodes in those fragments all have the entry node_to_block
           set to the block. In addition it is recorded to which fragment
           specifically the node belongs in the node_to_fragment dict.
        3) the node corresponds to a multiresidue block; but unlike in
           case to it represents multiple residues. ....

         Parameters
        ----------
        meta_molecule: polyply.src.meta_molecule.MetaMolecule
            The meta molecule to process.

        """
        regular_graph = nx.Graph()
        restart_graph = nx.Graph()
        restart_attr = nx.get_node_attributes(meta_molecule, "from_itp")

        # this breaks down when to proteins are directly linked
        # because they would appear as one connected component
        # and not two seperate components referring to two molecules
        # but that is an edge-case we can worry about later
        for idx, jdx in nx.dfs_edges(meta_molecule):
            # the two nodes are restart nodes
            if idx in restart_attr and jdx in restart_attr:
                restart_graph.add_edge(idx, jdx)
            else:
                regular_graph.add_edge(idx, jdx)

        # regular nodes have to match a block in the force-field by resname
        for node in regular_graph.nodes:
            self.node_to_block[node] = meta_molecule.nodes[node]["resname"]

        # fragment nodes match parts of blocks, which describe molecules
        # with more than one residue
        for fragment in nx.connected_components(restart_graph):
            block_name = restart_attr[list(fragment)[0]]
            if all([restart_attr[node] == block_name  for node in fragment]):
                self.fragments.append(fragment)
                block = self.force_field.blocks[block_name]
                for node in fragment:
                    self.node_to_block[node] = block_name
                    self.node_to_fragment[node] = len(self.fragments) - 1
            else:
                raise IOError

    def add_blocks(self, meta_molecule):
        """
        Add disconnected blocks to :class:`vermouth.molecule.Moleclue`
        and set the graph attribute to meta_molecule matching the node
        with the underlying fragment it represents at higher resolution.
        Note that this function also takes care to properly add multi-
        residue blocks (i.e. from an existing itp-file).

        Parameters
        ----------
        meta_molecule: polyply.src.meta_molecule.MetaMolecule
            The meta molecule to process.

        Returns
        -------
        vermouth.molecule.Molecule
            The disconnected fine-grained molecule.
        """
        # get a defined order for looping over the resiude graph
        node_keys = list(meta_molecule.nodes())
        resid_dict = nx.get_node_attributes(meta_molecule, "resid")
        resids = [resid_dict[node] for node in node_keys]
        node_keys = [x for _, x in sorted(zip(resids, node_keys))]
        # get the first node and convert it to molecule
        start_node = node_keys[0]
        new_mol = self.force_field.blocks[self.node_to_block[start_node]].to_molecule()

        # in this case the node belongs to a fragment for which there is a
        # multiresidue block
        if "from_itp" in meta_molecule.nodes[start_node]:
            # add all nodes of that fragment to added_fragment nodes
            fragment_nodes = list(self.fragments[self.node_to_fragment[start_node]])
            self.added_fragment_nodes += fragment_nodes

            # extract the nodes of this paticular residue and store a
            # dummy correspndance
            correspondence = {node:node for node in new_mol.nodes}
            self.multiblock_correspondence.append({node:node for node in new_mol.nodes})
            residue = _correspondence_to_residue(meta_molecule,
                                                 new_mol,
                                                 correspondence,
                                                 start_node)
            # add residue to meta_molecule node
            meta_molecule.nodes[start_node]["graph"] = residue
        else:
            # we store the block together with the residue node
            meta_molecule.nodes[start_node]["graph"] = new_mol.copy()

        # now we loop over the rest of the nodes
        for node in node_keys[1:]:
            # in this case the node belongs to a fragment which has been added
            # we only extract the residue belonging to this paticular node
            if node in self.added_fragment_nodes:
                fragment_id = self.node_to_fragment[node]
                correspondence = self.multiblock_correspondence[fragment_id]
            # in this case we have to add the node from the block definitions
            else:
                block = self.force_field.blocks[self.node_to_block[node]]
                correspondence = new_mol.merge_molecule(block)

            # make the residue from the correspondence
            residue = _correspondence_to_residue(meta_molecule,
                                                 new_mol,
                                                 correspondence,
                                                 node)
            # add residue to node
            meta_molecule.nodes[node]["graph"] = residue

            # in this case we just added a new multiblock residue so we store
            # the correspondence as well as keep track of the nodes that are
            # part of that fragment
            if "from_itp" in meta_molecule.nodes[node] and node not in self.added_fragments:
                fragment_nodes = list(self.fragments[self.node_to_fragment[start_node]])
                self.added_fragment_nodes += fragment_nodes
                self.multiblock_correspondence.append(correspondence)


        return new_mol

    def run_molecule(self, meta_molecule):
        """
        Take a meta_molecule and generated a disconnected graph
        of the higher resolution molecule by matching the resname
        attribute to blocks in the force-field. This function
        also takes care to correcly add parameters from an itp
        file to fine-grained molecule. It also sets the 'graph'
        attribute, which is the higher-resolution fragment that
        the meta_molecule node represents.

        Parameters
        ----------
        molecule: polyply.src.meta_molecule.MetaMolecule
             The meta molecule to process.

        Returns
        -------
        molecule: polyply.src.meta_molecule.MetaMolecule
            The meta molecule with attribute molecule that is the
            fine grained molecule.
        """
        # in a first step we match the residue names to blocks
        # in the force-field. Residue names can also be part
        # of a larger fragment stored as a block or refer to
        # a block which consists of multiple residues. This
        # gets entangled here
        self.match_nodes_to_blocks(meta_molecule)
        # next we check if all exclusions are the same and if
        # not we adjust it such that the lowest exclusion number
        # is used. ApplyLinks then generates those appropiately
        tag_exclusions(self.node_to_block, self.force_field)
        # now we add the blocks generating a new molecule
        new_molecule = self.add_blocks(meta_molecule)
        meta_molecule.molecule = new_molecule
        return meta_molecule
