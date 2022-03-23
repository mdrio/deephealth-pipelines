#!/usr/bin/env python
# -*- coding: utf-8 -*-

import abc
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal, NewType, TypedDict, Union

import cwl_utils.parser as cwl_parser
import networkx as nx
from pipe import map

DockerImage = NewType("DockerImage", str)
Output = NewType("Output", str)
Binding = NewType("Binding", Dict[str, "WorkflowElement"])


#  @dataclass
#  class Provenance:
#      target: Path
#      command: str
#      params: Dict
#      parent: "Provenance" = None
#


class Workflow(abc.ABC):
    @abc.abstractmethod
    def inputs(self, name: str = None) -> Union["InOut", List["InOut"]]:
        ...

    @abc.abstractmethod
    def outputs(self, name: str = None) -> Union["InOut", List["InOut"]]:
        ...

    @abc.abstractmethod
    def steps(self, name: str = None) -> Union["WorkflowStep", List["WorkflowStep"]]:
        ...

    @abc.abstractmethod
    def nodes(
        self, name: str = None
    ) -> Union["WorkflowElement", List["WorkflowElement"]]:
        ...


class WorkflowElement(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self) -> str:
        ...

    @property
    @abc.abstractmethod
    def in_binding(self) -> Binding:
        ...

    @property
    @abc.abstractmethod
    def out_binding(self) -> Binding:
        ...

    def __repr__(self):
        return f"<{self.name}>"

    def __eq__(self, other):
        return self.name == other.name


class InOut(WorkflowElement, abc.ABC):
    ...


class WorkflowStep(WorkflowElement):
    @property
    @abc.abstractmethod
    def in_binding(self) -> Binding:
        ...

    @property
    @abc.abstractmethod
    def out_binding(self) -> Binding:
        ...

    @property
    @abc.abstractmethod
    def params(self) -> List[str]:
        ...

    @property
    @abc.abstractmethod
    def outputs(self) -> List[str]:
        ...


class NXWorkflow(Workflow):
    def __init__(self, dag: nx.DiGraph):
        self._dag = dag
        self._outputs: Dict[str, InOut] = None
        self._inputs: Dict[str, InOut] = None
        self._steps: Dict[str, WorkflowStep] = None

    def outputs(self, name: str = None) -> Union["InOut", List["InOut"]]:
        if self._outputs is None:
            self._outputs = {
                node: NXInOut(node, self._dag)
                for node in self._dag.nodes
                if self._dag.out_degree(node) == 0
            }

        if name:
            return self._outputs[name]
        return list(self._outputs.values())

    def inputs(self, name: str = None) -> Union["InOut", List["InOut"]]:
        if self._inputs is None:
            self._inputs = {
                node: NXInOut(node, self._dag)
                for node in self._dag.nodes
                if self._dag.in_degree(node) == 0
            }
        if name:
            return self._inputs[name]
        return list(self._inputs.values())

    def steps(self, name: str = None) -> Union["WorkflowStep", List["WorkflowStep"]]:
        if self._steps is None:
            self._steps = {
                node: NXWorkflowStep(node, self._dag)
                for node, data in self._dag.nodes(data=True)
                if data.get("type") == "step"
            }
        if name:
            return self._steps[name]
        return list(self._steps.values())

    def nodes(
        self, name: str = None
    ) -> Union["WorkflowElement", List["WorkflowElement"]]:
        nodes = {
            node: NXWorkflowStep(node, self._dag)
            if data.get("type") == "step"
            else NXInOut(node, self._dag)
            for node, data in self._dag.nodes(data=True)
        }
        if name:
            return nodes[name]
        return list(nodes.values())


class NXWorkflowElement(WorkflowElement):
    def __init__(self, name: str, dag: nx.DiGraph):
        self._name = name
        self._dag = dag

    @property
    def name(self) -> str:
        return self._name

    @property
    def in_binding(self) -> Binding:
        binding: Binding = {}
        for edge in self._dag.in_edges(self.name, data=True):
            start, _, data = edge
            label = data["label"].split("/")[1]
            self._add_binding(binding, label, start)
        return binding

    @property
    def out_binding(self) -> Binding:
        binding: Binding = {}
        for edge in self._dag.edges(self.name, data=True):
            _, end, data = edge
            label = data["label"].split("/")[1]
            self._add_binding(binding, label, end)
        return binding

    @abc.abstractmethod
    def _add_binding(self, binding: Binding, label: str, node: str):
        ...


class NXInOut(InOut, NXWorkflowElement):
    def _add_binding(self, binding: Binding, label: str, node: str):
        binding[label] = NXWorkflowStep(node, self._dag)


class NXWorkflowStep(NXWorkflowElement, WorkflowStep):
    @property
    def params(self) -> List[str]:
        return [label for _, _, label in self._dag.in_edges(self.name, data=True)]

    @property
    def outputs(self) -> List[str]:
        return [label for _, _, label in self._dag.edges(self.name, data=True)]

    def _add_binding(self, binding: Binding, label: str, node: str):
        binding[label] = NXInOut(node, self._dag)


class WorkflowFactory(abc.ABC):
    @abc.abstractmethod
    def get(self):
        ...


CWLElement = Literal["inputs", "outpus", "steps"]


@dataclass
class NXWorkflowFactory(WorkflowFactory):
    cwl_workflow: cwl_parser.Workflow

    def get(self) -> Workflow:
        dag = self._get_dag()
        return NXWorkflow(dag)

    def _get_dag(self) -> nx.DiGraph:
        dag = nx.DiGraph()
        inputs = list(self.cwl_workflow.inputs | map(lambda x: self._get_id(x.id)))
        outputs = list(self.cwl_workflow.outputs | map(lambda x: self._get_id(x.id)))
        dag.add_nodes_from(inputs + outputs, type="inout")

        for step in self.cwl_workflow.steps:
            step_id = self._get_id(step.id)
            dag.add_node(step_id, type="step")

            for in_ in step.in_:
                dag.add_edge(
                    self._get_id(in_.source), step_id, label=self._get_id(in_.id)
                )
            for out in step.out:
                dest_node = out
                for cwl_out in self.cwl_workflow.outputs:
                    if cwl_out.outputSource == out:
                        dest_node = cwl_out.id
                        break
                dag.add_edge(step_id, self._get_id(dest_node), label=self._get_id(out))

        return dag

    #  def _get_params(self) -> List[str]:
    #      return map(
    #          self._get_id,
    #          self.cwl_workflow.inputs,
    #      )
    #
    #  def _get_outputs(self) -> Dict[str, WorkflowStep]:
    #      return dict(map(self._get_output, self.cwl_workflow.outputs))
    #
    #  def _get_output(self, cwl_output) -> Tuple[str, WorkflowStep]:
    #      _id = self._get_id(cwl_output)
    #      step_id = cwl_output.outputSource.replace(f"/{_id}", "")
    #      cwl_step = self._get_step_by_id(step_id)
    #      docker_img = list(
    #          filter(
    #              lambda x: isinstance(x, get_args(cwl_parser.DockerRequirement)),
    #              cwl_step.run.requirements,
    #          )
    #      )[0].dockerPull
    #      base_command = cwl_step.run.baseCommand
    #      params = self._get_step_params(cwl_step)
    #
    def _get_element_by_id(
        self, cwl_element: CWLElement, _id: str
    ) -> cwl_parser.WorkflowStep:
        return list(
            filter(lambda s: s.id == _id, getattr(self.cwl_workflow, cwl_element))
        )[0]

    #
    #  def _get_step_params(self, step: cwl_parser.WorkflowStep) -> List[WorkflowElement]:
    #       params = step.in_ | map(self._get_id) | map(lambda x: )
    #
    def _get_id(self, element) -> str:
        return element.split("#")[1] if "#" in element else element

    #
