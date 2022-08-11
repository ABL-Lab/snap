import json

import pytest
from libsonata import SonataError

import bluepysnap.circuit as test_module
from bluepysnap.edges import EdgePopulation, Edges
from bluepysnap.exceptions import BluepySnapError
from bluepysnap.nodes import NodePopulation, Nodes

from utils import TEST_DATA_DIR, copy_test_data, edit_config


def test_all():
    circuit = test_module.Circuit(str(TEST_DATA_DIR / "circuit_config.json"))
    assert circuit.config["networks"]["nodes"][0] == {
        "nodes_file": str(TEST_DATA_DIR / "nodes.h5"),
        "populations": {"default": None},
    }
    assert isinstance(circuit.nodes, Nodes)
    assert isinstance(circuit.edges, Edges)
    assert list(circuit.edges) == ["default", "default2"]
    assert isinstance(circuit.edges["default"], EdgePopulation)
    assert isinstance(circuit.edges["default2"], EdgePopulation)
    assert sorted(list(circuit.nodes)) == ["default", "default2"]
    assert isinstance(circuit.nodes["default"], NodePopulation)
    assert isinstance(circuit.nodes["default2"], NodePopulation)
    assert sorted(circuit.node_sets) == sorted(
        json.load(open(str(TEST_DATA_DIR / "node_sets.json")))
    )


def test_duplicate_node_populations():
    with copy_test_data() as (_, config_path):
        with edit_config(config_path) as config:
            config["networks"]["nodes"].append(config["networks"]["nodes"][0])

        with pytest.raises(SonataError, match="Duplicate population"):
            test_module.Circuit(config_path)


def test_duplicate_edge_populations():
    with copy_test_data() as (_, config_path):
        with edit_config(config_path) as config:
            config["networks"]["edges"].append(config["networks"]["edges"][0])

        with pytest.raises(SonataError, match="Duplicate population"):
            test_module.Circuit(config_path)


def test_no_node_set():
    with copy_test_data() as (_, config_path):
        with edit_config(config_path) as config:
            config.pop("node_sets_file")
        circuit = test_module.Circuit(config_path)
        assert circuit.node_sets == {}


def test_integration():
    circuit = test_module.Circuit(str(TEST_DATA_DIR / "circuit_config.json"))
    node_ids = circuit.nodes.ids({"mtype": ["L6_Y", "L2_X"]})
    edge_ids = circuit.edges.afferent_edges(node_ids)
    edge_props = circuit.edges.get(edge_ids, properties=["syn_weight", "delay"])
    edge_reduced = edge_ids.limit(2)
    edge_props_reduced = edge_props.loc[edge_reduced]
    assert edge_props_reduced["syn_weight"].tolist() == [1, 1]
