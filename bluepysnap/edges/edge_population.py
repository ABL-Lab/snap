# Copyright (c) 2019, EPFL/Blue Brain Project

# This file is part of BlueBrain SNAP library <https://github.com/BlueBrain/snap>

# This library is free software; you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License version 3.0 as published
# by the Free Software Foundation.

# This library is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.

# You should have received a copy of the GNU Lesser General Public License
# along with this library; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

"""Edge population access."""
import inspect
from collections.abc import Mapping

import libsonata
import numpy as np
import pandas as pd
from cached_property import cached_property
from more_itertools import first

from bluepysnap import query, utils
from bluepysnap.circuit_ids import CircuitEdgeIds, CircuitNodeId
from bluepysnap.circuit_ids_types import IDS_DTYPE, CircuitEdgeId
from bluepysnap.edges.edge_population_stats import StatsHelper
from bluepysnap.exceptions import BluepySnapError
from bluepysnap.sonata_constants import DYNAMICS_PREFIX, ConstContainer, Edge


def _is_empty(xs):
    return (xs is not None) and (len(xs) == 0)


def _estimate_range_size(func, node_ids, n=3):
    """Median size of index second level for some node IDs from the provided list."""
    assert len(node_ids) > 0
    if len(node_ids) > n:
        node_ids = np.random.choice(node_ids, size=n, replace=False)
    return np.median([len(func(node_id).ranges) for node_id in node_ids])


class EdgePopulation:
    """Edge population access."""

    def __init__(self, circuit, population_name):
        """Initializes a EdgePopulation object from a EdgeStorage and a population name.

        Args:
            circuit (bluepysnap.Circuit): the circuit object containing the edge population
            population_name (str): the name of the edge population

        Returns:
            EdgePopulation: An EdgePopulation object.
        """
        self._circuit = circuit
        self.name = population_name

    @cached_property
    def _properties(self):
        """Edge population properties."""
        return self._circuit.to_libsonata.edge_population_properties(self.name)

    @property
    def _population(self):
        """Libsonata edge population.

        Not cached because it would keep the hdf5 file open.
        """
        return self._circuit.to_libsonata.edge_population(self.name)

    @staticmethod
    def _resolve_node_ids(nodes, group):
        """Node IDs corresponding to node group filter."""
        if group is None:
            return None
        return nodes.ids(group)

    @property
    def size(self):
        """Population size."""
        return self._population.size

    def _nodes(self, population_name):
        """Returns the NodePopulation corresponding to population."""
        result = self._circuit.nodes[population_name]
        return result

    @property
    def type(self):
        """Population type."""
        return self._properties.type

    @cached_property
    def source(self):
        """Source NodePopulation."""
        return self._nodes(self._population.source)

    @cached_property
    def target(self):
        """Target NodePopulation."""
        return self._nodes(self._population.target)

    @cached_property
    def _attribute_names(self):
        return set(self._population.attribute_names)

    @cached_property
    def _dynamics_params_names(self):
        return set(utils.add_dynamic_prefix(self._population.dynamics_attribute_names))

    @property
    def _topology_property_names(self):
        return {Edge.SOURCE_NODE_ID, Edge.TARGET_NODE_ID}

    @property
    def config(self):
        """Access the configuration for the population.

        This configuration is extended with:

        * 'components' of the circuit config
        * 'edges_file': the path the h5 file containing the population.
        """
        return self._circuit.get_edge_population_config(self.name)

    @property
    def property_names(self):
        """Set of available edge properties.

        Notes:
            Properties are a combination of the group attributes, the dynamics_params and the
            topology properties.
        """
        return self._attribute_names | self._dynamics_params_names | self._topology_property_names

    @cached_property
    def property_dtypes(self):
        """Returns the dtypes of all the properties.

        Returns:
            pandas.Series: series indexed by field name with the corresponding dtype as value.
        """
        return self.get([0], list(self.property_names)).dtypes.sort_index()

    @cached_property
    def stats(self):
        """Access edge population stats methods."""
        return StatsHelper(self)

    def container_property_names(self, container):
        """Lists the ConstContainer properties shared with the EdgePopulation.

        Args:
            container (ConstContainer): a container class for edge properties.

        Returns:
            list: A list of strings corresponding to the properties that you can use from the
            container class

        Examples:
            >>> from bluepysnap.sonata_constants import Edge
            >>> print(my_edge_population.container_property_names(Edge))
            >>> ["AXONAL_DELAY", "SYN_WEIGHT"] # values you can use with my_edge_population
        """
        if not inspect.isclass(container) or not issubclass(container, ConstContainer):
            raise BluepySnapError("'container' must be a subclass of ConstContainer")
        in_file = self.property_names
        return [k for k in container.key_set() if container.get(k) in in_file]

    def _get_property(self, prop, selection):
        if prop == Edge.SOURCE_NODE_ID:
            result = utils.ensure_ids(self._population.source_nodes(selection))
        elif prop == Edge.TARGET_NODE_ID:
            result = utils.ensure_ids(self._population.target_nodes(selection))
        elif prop in self._attribute_names:
            result = self._population.get_attribute(prop, selection)
        elif prop in self._dynamics_params_names:
            result = self._population.get_dynamics_attribute(
                prop.split(DYNAMICS_PREFIX)[1], selection
            )
        else:
            raise BluepySnapError(f"No such property: {prop}")
        return result

    def _get(self, selection, properties=None):
        """Get an array of edge IDs or DataFrame with edge properties."""
        edge_ids = utils.ensure_ids(selection.flatten())
        if properties is None:
            return edge_ids

        if utils.is_iterable(properties):
            if len(edge_ids) == 0:
                result = pd.DataFrame(columns=properties)
            else:
                result = pd.DataFrame(index=edge_ids)
                for p in properties:
                    result[p] = self._get_property(p, selection)
        else:
            if len(edge_ids) == 0:
                result = pd.Series(name=properties, dtype=np.float64)
            else:
                result = pd.Series(
                    self._get_property(properties, selection), index=edge_ids, name=properties
                )

        return result

    def _edge_ids_by_filter(self, queries, raise_missing_prop):
        """Return edge IDs if their properties match the `queries` dict.

        `props` values could be:
            pairs (range match for floating dtype fields)
            scalar or iterables (exact or "one of" match for other fields)

        You can use the special operators '$or' and '$and' also to combine different queries
        together.

        Examples:
            >>> self._edge_ids_by_filter({ Edge.POST_SECTION_ID: (0, 1),
            >>>                            Edge.AXONAL_DELAY: (.5, 2.) })
            >>> self._edge_ids_by_filter({'$or': [{ Edge.PRE_X_CENTER: [2, 3]},
            >>>                              { Edge.POST_SECTION_POS: (0, 1),
            >>>                              Edge.SYN_WEIGHT: (0.,1.4) }]})

        """
        properties = query.get_properties(queries)
        unknown_props = properties - self.property_names
        if raise_missing_prop and unknown_props:
            raise BluepySnapError(f"Unknown edge properties: {unknown_props}")
        res = []
        ids = self.ids(None)
        chunk_size = int(1e8)
        for chunk in np.array_split(ids, 1 + len(ids) // chunk_size):
            data = self.get(chunk, properties - unknown_props)
            res.extend(chunk[query.resolve_ids(data, self.name, self.type, queries)])
        return np.array(res, dtype=IDS_DTYPE)

    def ids(self, group=None, limit=None, sample=None, raise_missing_property=True):
        """Edge IDs corresponding to edges ``edge_ids``.

        Args:
            group (None/int/CircuitEdgeId/CircuitEdgeIds/sequence): Which IDs will be
                returned depends on the type of the ``group`` argument:

                - ``None``: return all IDs.
                - ``int``, ``CircuitEdgeId``: return a single edge ID.
                - ``CircuitEdgeIds`` return IDs of edges the edge population in an array.
                - ``sequence``: return IDs of edges in an array.

            sample (int): If specified, randomly choose ``sample`` number of
                IDs from the match result. If the size of the sample is greater than
                the size of the EdgePopulation then all ids are taken and shuffled.

            limit (int): If specified, return the first ``limit`` number of
                IDs from the match result. If limit is greater than the size of the population
                all node IDs are returned.

            raise_missing_property (bool): if True, raises if a property is not listed in this
                population. Otherwise the ids are just not selected if a property is missing.

        Returns:
            numpy.array: A numpy array of IDs.
        """
        if group is None:
            result = self._population.select_all().flatten()
        elif isinstance(group, CircuitEdgeIds):
            result = group.filter_population(self.name).get_ids()
        elif isinstance(group, np.ndarray):
            result = group
        elif isinstance(group, Mapping):
            result = self._edge_ids_by_filter(
                queries=group, raise_missing_prop=raise_missing_property
            )
        else:
            result = utils.ensure_list(group)
            # test if first value is a CircuitEdgeId if yes then all values must be CircuitEdgeId
            if isinstance(first(result, None), CircuitEdgeId):
                try:
                    result = [cid.id for cid in result if cid.population == self.name]
                except AttributeError as e:
                    raise BluepySnapError(
                        "All values from a list must be of type int or CircuitEdgeId."
                    ) from e
        if sample is not None:
            if len(result) > 0:
                result = np.random.choice(result, min(sample, len(result)), replace=False)
        if limit is not None:
            result = result[:limit]
        return utils.ensure_ids(result)

    def get(self, edge_ids, properties=None):
        """Edge properties as pandas DataFrame.

        Args:
            edge_ids (array-like): array-like of edge IDs
            properties (str/list): an edge property name or a list of edge property names

        Returns:
            pandas.Series/pandas.DataFrame:
                - A pandas Series indexed by edge IDs if ``properties`` is scalar.
                - A pandas DataFrame indexed by edge IDs if ``properties`` is list.

        Notes:
            The EdgePopulation.property_names function will give you all the usable properties
            for the `properties` argument.
        """
        edge_ids = self.ids(edge_ids)
        selection = libsonata.Selection(edge_ids)
        return self._get(selection, properties)

    def positions(self, edge_ids, side, kind):
        """Edge positions as a pandas DataFrame.

        Args:
            edge_ids (array-like): array-like of edge IDs
            side (str): ``afferent`` or ``efferent``
            kind (str): ``center`` or ``surface``

        Returns:
            Pandas Dataframe with ('x', 'y', 'z') columns indexed by edge IDs.
        """
        assert side in ("afferent", "efferent")
        assert kind in ("center", "surface")
        props = {f"{side}_{kind}_{p}": p for p in ["x", "y", "z"]}
        result = self.get(edge_ids, list(props))
        result.rename(columns=props, inplace=True)
        result.sort_index(axis=1, inplace=True)
        return result

    def afferent_nodes(self, target, unique=True):
        """Get afferent node IDs for given target ``node_id``.

        Notes:
            Afferent nodes are nodes projecting an outgoing edge to one of the ``target`` node.

        Args:
            target (CircuitNodeIds/int/sequence/str/mapping/None): the target you want to resolve
            and use as target nodes.
            unique (bool): If ``True``, return only unique afferent node IDs.

        Returns:
            numpy.ndarray: Afferent node IDs for all the targets.
        """
        if target is not None:
            selection = self._population.afferent_edges(self._resolve_node_ids(self.target, target))
        else:
            selection = self._population.select_all()
        result = self._population.source_nodes(selection)
        if unique:
            result = np.unique(result)
        return utils.ensure_ids(result)

    def efferent_nodes(self, source, unique=True):
        """Get efferent node IDs for given source ``node_id``.

        Notes:
            Efferent nodes are nodes receiving an incoming edge from one of the ``source`` node.

        Args:
            source (CircuitNodeIds/int/sequence/str/mapping/None): the source you want to resolve
                and use as source nodes.
            unique (bool): If ``True``, return only unique afferent node IDs.

        Returns:
            numpy.ndarray: Efferent node IDs for all the sources.
        """
        if source is not None:
            selection = self._population.efferent_edges(self._resolve_node_ids(self.source, source))
        else:
            selection = self._population.select_all()
        result = self._population.target_nodes(selection)
        if unique:
            result = np.unique(result)
        return utils.ensure_ids(result)

    def pathway_edges(self, source=None, target=None, properties=None):
        """Get edges corresponding to ``source`` -> ``target`` connections.

        Args:
            source (CircuitNodeIds/int/sequence/str/mapping/None): source node group
            target (CircuitNodeIds/int/sequence/str/mapping/None): target node group
            properties: None / edge property name / list of edge property names

        Returns:
            - List of edge IDs, if ``properties`` is None;
            - Pandas Series indexed by edge IDs if ``properties`` is string;
            - Pandas DataFrame indexed by edge IDs if ``properties`` is list.
        """
        if source is None and target is None:
            raise BluepySnapError("Either `source` or `target` should be specified")

        source_node_ids = self._resolve_node_ids(self.source, source)
        target_edge_ids = self._resolve_node_ids(self.target, target)

        if source_node_ids is None:
            selection = self._population.afferent_edges(target_edge_ids)
        elif target_edge_ids is None:
            selection = self._population.efferent_edges(source_node_ids)
        else:
            selection = self._population.connecting_edges(source_node_ids, target_edge_ids)

        if properties:
            return self._get(selection, properties)
        return utils.ensure_ids(selection.flatten())

    def afferent_edges(self, node_id, properties=None):
        """Get afferent edges for given ``node_id``.

        Args:
            node_id (CircuitNodeIds/int/sequence/str/mapping/None) : Target node ID.
            properties: An edge property name, a list of edge property names, or None.

        Returns:
            pandas.Series/pandas.DataFrame/list:
                - A pandas Series indexed by edge ID if ``properties`` is a string.
                - A pandas DataFrame indexed by edge ID if ``properties`` is a list.
                - A list of edge IDs, if ``properties`` is None.
        """
        return self.pathway_edges(source=None, target=node_id, properties=properties)

    def efferent_edges(self, node_id, properties=None):
        """Get efferent edges for given ``node_id``.

        Args:
            node_id (CircuitNodeIds/int/sequence/str/mapping/None): source node ID
            properties: None / edge property name / list of edge property names

        Returns:
            - List of edge IDs, if ``properties`` is None;
            - Pandas Series indexed by edge IDs if ``properties`` is string;
            - Pandas DataFrame indexed by edge IDs if ``properties`` is list.
        """
        return self.pathway_edges(source=node_id, target=None, properties=properties)

    def pair_edges(self, source_node_id, target_node_id, properties=None):
        """Get edges corresponding to ``source_node_id`` -> ``target_node_id`` connection.

        Args:
            source_node_id (CircuitNodeIds/int/sequence/str/mapping/None): source node ID
            target_node_id (CircuitNodeIds/int/sequence/str/mapping/None): target node ID
            properties: None / edge property name / list of edge property names

        Returns:
            - List of edge IDs, if ``properties`` is None;
            - Pandas Series indexed by edge IDs if ``properties`` is string;
            - Pandas DataFrame indexed by edge IDs if ``properties`` is list.
        """
        return self.pathway_edges(
            source=source_node_id, target=target_node_id, properties=properties
        )

    def _iter_connections(self, source_node_ids, target_node_ids, unique_node_ids, shuffle):
        """Iterate through `source_node_ids` -> `target_node_ids` connections."""

        # pylint: disable=too-many-branches,too-many-locals
        def _optimal_direction():
            """Choose between source and target node IDs for iterating."""
            if target_node_ids is None and source_node_ids is None:
                raise BluepySnapError("Either `source` or `target` should be specified")
            if source_node_ids is None:
                return "target"
            if target_node_ids is None:
                return "source"
            else:
                # Checking the indexing 'direction'. One direction has contiguous indices.
                range_size_source = _estimate_range_size(
                    self._population.efferent_edges, source_node_ids
                )
                range_size_target = _estimate_range_size(
                    self._population.afferent_edges, target_node_ids
                )
                return "source" if (range_size_source < range_size_target) else "target"

        if _is_empty(source_node_ids) or _is_empty(target_node_ids):
            return

        direction = _optimal_direction()
        if direction == "target":
            primary_node_ids, secondary_node_ids = target_node_ids, source_node_ids
            get_connected_node_ids = self.afferent_nodes
        else:
            primary_node_ids, secondary_node_ids = source_node_ids, target_node_ids
            get_connected_node_ids = self.efferent_nodes

        primary_node_ids = np.unique(primary_node_ids)
        if shuffle:
            np.random.shuffle(primary_node_ids)

        if secondary_node_ids is not None:
            secondary_node_ids = np.unique(secondary_node_ids)

        secondary_node_ids_used = set()

        for key_node_id in primary_node_ids:
            connected_node_ids = get_connected_node_ids(key_node_id, unique=False)
            # [[secondary_node_id, count], ...]
            connected_node_ids_with_count = np.stack(
                np.unique(connected_node_ids, return_counts=True)
            ).transpose()
            # np.stack(uint64, int64) -> float64
            connected_node_ids_with_count = connected_node_ids_with_count.astype(np.uint32)
            if secondary_node_ids is not None:
                mask = np.isin(
                    connected_node_ids_with_count[:, 0], secondary_node_ids, assume_unique=True
                )
                connected_node_ids_with_count = connected_node_ids_with_count[mask]
            if shuffle:
                np.random.shuffle(connected_node_ids_with_count)

            for conn_node_id, edge_count in connected_node_ids_with_count:
                if unique_node_ids and (conn_node_id in secondary_node_ids_used):
                    continue
                if direction == "target":
                    yield conn_node_id, key_node_id, edge_count
                else:
                    yield key_node_id, conn_node_id, edge_count
                if unique_node_ids:
                    secondary_node_ids_used.add(conn_node_id)
                    break

    def _add_circuit_ids(self, its):
        """Completes the CircuitNodeId."""

        return (
            (
                CircuitNodeId(self.source.name, source_id),
                CircuitNodeId(self.target.name, target_id),
                count,
            )
            for source_id, target_id, count in its
        )

    def _add_edge_ids(self, its):
        """Completes the CircuitNodeId and adds the CircuitEdgeIds."""

        return (
            (
                CircuitNodeId(self.source.name, source_id),
                CircuitNodeId(self.target.name, target_id),
                CircuitEdgeIds.from_dict({self.name: self.pair_edges(source_id, target_id)}),
            )
            for source_id, target_id, _ in its
        )

    def _omit_edge_count(self, its):
        """Completes the CircuitNodeId and removes the edge count."""

        return (
            (
                CircuitNodeId(self.source.name, source_id),
                CircuitNodeId(self.target.name, target_id),
            )
            for source_id, target_id, _ in its
        )

    def iter_connections(
        self,
        source=None,
        target=None,
        unique_node_ids=False,
        shuffle=False,
        return_edge_ids=False,
        return_edge_count=False,
    ):
        """Iterate through ``source`` -> ``target`` connections.

        Args:
            source (CircuitNodeIds/int/sequence/str/mapping/None): source node group
            target (CircuitNodeIds/int/sequence/str/mapping/None): target node group
            unique_node_ids: if True, no node ID will be used more than once as source or
                target for edges. Careful, this flag does not provide unique (source, target)
                pairs but unique node IDs.
            shuffle: if True, result order would be (somewhat) randomized
            return_edge_count: if True, edge count is added to yield result
            return_edge_ids: if True, edge ID list is added to yield result

        ``return_edge_count`` and ``return_edge_ids`` are mutually exclusive.

        Yields:
            - (source_node_id, target_node_id, edge_ids) if ``return_edge_ids`` is True;
            - (source_node_id, target_node_id, edge_count) if ``return_edge_count`` is True;
            - (source_node_id, target_node_id) otherwise.
        """
        if return_edge_ids and return_edge_count:
            raise BluepySnapError(
                "`return_edge_count` and `return_edge_ids` are mutually exclusive"
            )

        source_node_ids = self._resolve_node_ids(self.source, source)
        target_node_ids = self._resolve_node_ids(self.target, target)

        it = self._iter_connections(source_node_ids, target_node_ids, unique_node_ids, shuffle)

        if return_edge_count:
            return self._add_circuit_ids(it)
        elif return_edge_ids:
            return self._add_edge_ids(it)
        else:
            return self._omit_edge_count(it)

    @property
    def h5_filepath(self):
        """Get the H5 edges file associated with population."""
        return self.config["edges_file"]

    @cached_property
    def spatial_synapse_index(self):
        """Access to edges spatial index."""
        from brain_indexer import open_index

        index_dir = self._properties.spatial_synapse_index_dir
        if not index_dir:
            raise BluepySnapError(f"It appears {self.name} does not have synapse indices")
        return open_index(index_dir)

    def __getstate__(self):
        """Make EdgePopulation pickle-able, without storing state of caches."""
        return self._circuit, self.name

    def __setstate__(self, state):
        """Load from pickle state."""
        circuit, population_name = state
        self.__init__(circuit, population_name)
