#!/usr/bin/env python
# -*- coding: utf-8 -*-
import json
import logging
import os
import shutil
import time
from datetime import datetime
from enum import Enum
from typing import Dict
from uuid import uuid4

import requests
import yaml
from airflow import DAG
from airflow.api.common.experimental.trigger_dag import trigger_dag
from airflow.decorators import task
from airflow.exceptions import AirflowException
from airflow.hooks.base import BaseHook
from airflow.models import DagRun, Variable
from airflow.operators.python import get_current_context
from airflow.utils import timezone
from airflow.utils.state import State
from airflow.utils.task_group import TaskGroup
from airflow.utils.types import DagRunType
from utils import check_gpus_available, copy_slide, docker_run, move_slide

logger = logging.getLogger("watch-dir")

OME_SEADRAGON_REGISTER_SLIDE = Variable.get("OME_SEADRAGON_REGISTER_SLIDE")
OME_SEADRAGON_URL = Variable.get("OME_SEADRAGON_URL")

PROMORT_CONNECTION = BaseHook.get_connection("promort")
PROMORT_SESSION_ID = Variable.get("PROMORT_SESSION_ID")

PREDICTIONS_DIR = Variable.get("PREDICTIONS_DIR")
INPUT_DIR = Variable.get("INPUT_DIR")
STAGE_DIR = Variable.get("STAGE_DIR")
FAILED_DIR = Variable.get("FAILED_DIR")
BACKUP_DIR = Variable.get("BACKUP_DIR")

DOCKER_NETWORK = Variable.get("DOCKER_NETWORK", default_var="")

PROMORT_TOOLS_IMG = Variable.get("PROMORT_TOOLS_IMG")


def handle_error(ctx):
    slide = ctx["params"]["slide"]
    move_slide(os.path.join(STAGE_DIR, slide), FAILED_DIR)
    _remove_slide_from_omero(slide)


def _remove_slide_from_omero(slide):
    logger.info("removing slide %s", slide)
    slide_no_ext = os.path.splitext(slide)[0]
    url = requests.compat.urljoin(
        OME_SEADRAGON_URL, f"ome_seadragon/mirax/delete_files/{slide_no_ext}"
    )
    response = requests.get(url)
    logger.info("response.text %s", response.text)
    response.raise_for_status()


default_args = {
    "owner": "airflow",
    "depends_on_past": False,
    "email": ["airflow@example.com"],
    "start_date": datetime(2019, 1, 1),
    "email_on_failure": False,
    "email_on_retry": False,
    "retries": 1,
    "on_failure_callback": handle_error,
}


def create_dag():
    with DAG(
        "pipeline",
        on_failure_callback=handle_error,
        schedule_interval=None,
        max_active_runs=1,
        default_args=default_args,
    ) as dag:

        with TaskGroup(group_id="add_slide_to_backend"):
            slide = prepare_data()
            slide_info_ = add_slide_to_omero(slide)
            slide = slide_info_["slide"]
            slide_to_promort = add_slide_to_promort(slide_info_)

        dag_info = predictions()
        slide_to_promort >> dag_info
        generate_rocrate(gather_report(dag_info))

        for prediction in Prediction:
            with TaskGroup(group_id=f"add_{prediction.value}_to_backend"):
                prediction_info = task(
                    add_prediction_to_omero, task_id=f"add_{prediction.value}_to_omero"
                )(prediction, dag_info)
                prediction_label = prediction_info["label"]
                prediction_path = prediction_info["path"]
                omero_id = str(prediction_info["omero_id"])

                prediction_id = task(
                    add_prediction_to_promort,
                    task_id=f"add_{prediction.value}_to_promort",
                )(prediction.value, slide, prediction_label, omero_id)

                if prediction == Prediction.TUMOR:
                    tumor_branch(prediction_label, prediction, slide)
                elif prediction == Prediction.TISSUE:
                    tissue_branch(prediction_label, prediction_path, prediction_id)
        return dag


@task
def prepare_data():
    slide = get_current_context()["params"]["slide"]
    try:
        copy_slide(os.path.join(INPUT_DIR, slide), BACKUP_DIR)
    except FileExistsError:
        logger.info("slide %s already in backup", slide)

    move_slide(os.path.join(INPUT_DIR, slide), STAGE_DIR)
    return slide


@task(multiple_outputs=True)
def add_slide_to_omero(slide) -> Dict[str, str]:
    slide_name = os.path.splitext(slide)[0]
    response = requests.get(
        OME_SEADRAGON_REGISTER_SLIDE, params={"slide_name": slide_name}
    )

    logger.info("response.text %s", response.text)
    response.raise_for_status()
    omero_id = response.json()["mirax_index_omero_id"]

    return {"slide": slide_name, "omero_id": omero_id}


@task
def predictions() -> Dict[str, str]:
    slide = get_current_context()["params"]["slide"]
    allowed_states = [State.SUCCESS]
    failed_states = [State.FAILED]
    params_to_update = get_current_context()["params"]["params"]
    mode = params_to_update.get("mode") or Variable.get("PREDICTIONS_MODE")
    if mode == "serial":
        params = Variable.get("SERIAL_PREDICTIONS_PARAMS", deserialize_json=True)
    else:
        params = Variable.get("PARALLEL_PREDICTIONS_PARAMS", deserialize_json=True)

    params.update(params_to_update)
    params["slide"]["path"] = slide
    if "gpu" in params:
        gpus = os.environ["CWLDOCKER_GPUS"]
        check_gpus_available(gpus)

    execution_date = timezone.utcnow()
    triggered_run_id = DagRun.generate_run_id(DagRunType.MANUAL, execution_date)
    triggered_run_id = f"{slide}-{triggered_run_id}"

    logger.info("triggering dag with id %s", triggered_run_id)
    dag_id = "predictions"
    dag_run = trigger_dag(
        dag_id=dag_id,
        run_id=triggered_run_id,
        execution_date=execution_date,
        conf={"job": params},
        replace_microseconds=False,
    )
    while True:
        time.sleep(10)

        dag_run.refresh_from_db()
        state = dag_run.state
        if state in failed_states:
            raise AirflowException(f"{dag_id} failed with failed states {state}")
        if state in allowed_states:
            return {"dag_id": dag_id, "dag_run_id": triggered_run_id}


@task
def add_slide_to_promort(slide_info: Dict[str, str]):

    slide = os.path.splitext(slide_info["slide"])[0]
    omero_id = slide_info["omero_id"]
    command = [
        PROMORT_TOOLS_IMG,
        "importer.py",
        "--host",
        f"{PROMORT_CONNECTION.conn_type}://{PROMORT_CONNECTION.host}:{PROMORT_CONNECTION.port}",
        "--user",
        PROMORT_CONNECTION.login,
        "--passwd",
        PROMORT_CONNECTION.password,
        "--session-id",
        PROMORT_SESSION_ID,
        "slides_importer",
        "--slide-label",
        slide,
        "--extract-case",
        "--omero-id",
        str(omero_id),
        "--mirax",
        "--omero-host",
        OME_SEADRAGON_URL,
        "--ignore-duplicated",
    ]
    docker_run(command, DOCKER_NETWORK)


def add_prediction_to_omero(prediction, dag_info) -> Dict[str, str]:
    dag_id, dag_run_id = dag_info["dag_id"], dag_info["dag_run_id"]
    logger.info(
        "register prediction %s to omero with dag_id %s, dag_run_id %s",
        prediction.value,
        dag_id,
        dag_run_id,
    )
    output_dir = _get_output_dir(dag_id, dag_run_id)
    location = _get_prediction_location(prediction, output_dir)
    dest = _move_prediction_to_omero_dir(location)
    return _register_prediction_to_omero(
        os.path.basename(dest), prediction == Prediction.TUMOR
    )


def add_prediction_to_promort(
    prediction,
    slide_label: str,
    prediction_label: str,
    omero_id: str,
    review_required: bool = False,
) -> str:

    command = [
        PROMORT_TOOLS_IMG,
        "importer.py",
        "--host",
        f"{PROMORT_CONNECTION.conn_type}://{PROMORT_CONNECTION.host}:{PROMORT_CONNECTION.port}",
        "--user",
        PROMORT_CONNECTION.login,
        "--passwd",
        PROMORT_CONNECTION.password,
        "--session-id",
        PROMORT_SESSION_ID,
        "predictions_importer",
        "--prediction-label",
        prediction_label,
        "--slide-label",
        slide_label,
        "--prediction-type",
        prediction.upper(),
        "--omero-id",
        omero_id,
    ]
    if review_required:
        command.append("--review-required")

    logger.info("command %s", command)
    res = docker_run(command, DOCKER_NETWORK)
    return json.loads(res)["id"]


@task
def convert_to_tiledb(dataset_label):

    command = [
        "-v",
        f"{PREDICTIONS_DIR}:/data",
        PROMORT_TOOLS_IMG,
        "zarr_to_tiledb.py",
        "--zarr-dataset",
        f"/data/{dataset_label}",
        "--out-folder",
        "/data",
    ]
    return docker_run(command, DOCKER_NETWORK)


def tumor_branch(prediction_label, prediction, slide_label):

    register_to_omero = task(
        _register_prediction_to_omero,
        task_id=f"add_{prediction.value}.tiledb_to_omero",
    )(f"{prediction_label}.tiledb", False)

    omero_id = str(register_to_omero["omero_id"])
    register_to_promort = task(
        add_prediction_to_promort,
        task_id=f"add_{prediction.value}.tiledb_to_promort",
    )(
        prediction.value,
        slide_label,
        f"{prediction_label}.tiledb",
        omero_id,
        review_required=True,
    )

    convert_to_tiledb(prediction_label) >> register_to_omero >> register_to_promort


def tissue_branch(dataset_label, dataset_path, prediction_id):
    #  TODO add variable for threshold
    shapes_filename = tissue_segmentation(dataset_label, dataset_path)
    create_tissue_fragments(prediction_id, shapes_filename)


@task
def tissue_segmentation(label, path) -> str:
    threshold = Variable.get("ROI_THRESHOLD")
    out = os.path.join(PREDICTIONS_DIR, f"{label}_shapes.json")

    command = [
        "-v",
        f"{PREDICTIONS_DIR}:{PREDICTIONS_DIR}",
        PROMORT_TOOLS_IMG,
        "mask_to_shapes.py",
        f"{PREDICTIONS_DIR}/{os.path.basename(path)}",
        "-t",
        str(threshold),
        "-o",
        out,
        "--scale-func",
        "shapely",
    ]
    docker_run(command, DOCKER_NETWORK)
    return out


@task
def create_tissue_fragments(prediction_id, shapes_filename):

    command = [
        "-v",
        f"{PREDICTIONS_DIR}:{PREDICTIONS_DIR}",
        PROMORT_TOOLS_IMG,
        "importer.py",
        "--host",
        f"{PROMORT_CONNECTION.conn_type}://{PROMORT_CONNECTION.host}:{PROMORT_CONNECTION.port}",
        "--user",
        PROMORT_CONNECTION.login,
        "--passwd",
        PROMORT_CONNECTION.password,
        "--session-id",
        PROMORT_SESSION_ID,
        "tissue_fragments_importer",
        "--prediction-id",
        str(prediction_id),
        shapes_filename,
    ]
    docker_run(command, network=DOCKER_NETWORK)


class Prediction(Enum):
    TISSUE = "tissue"
    TUMOR = "tumor"


def _get_prediction_path(prediction: Prediction, dag_id: str, dag_run_id: str) -> str:
    output_dir = _get_output_dir(dag_id, dag_run_id)

    with open(os.path.join(output_dir, "workflow_report.json"), "r") as report_file:
        report = json.load(report_file)

    return report[prediction.value]["location"].replace("file://", "")


def _get_output_dir(dag_id, dag_run_id):
    return (
        os.path.join(Variable.get("OUT_DIR"), dag_id, dag_run_id)
        .replace(":", "_")
        .replace("+", "_")
    )


def _move_prediction_to_omero_dir(location):
    dest = os.path.join(PREDICTIONS_DIR, f"{str(uuid4())}.zip")
    logger.info("moving %s to %s", location, dest)
    # @fixme change to move
    shutil.copy(location, dest)
    return dest


def _register_prediction_to_omero(label, extract_archive) -> Dict[str, str]:
    logger.info(
        "register_prediction_to_omero: label %s, extract_archive %s",
        label,
        extract_archive,
    )
    ome_seadragon_register_predictions = Variable.get(
        "OME_SEADRAGON_REGISTER_PREDICTIONS"
    )

    response = requests.get(
        ome_seadragon_register_predictions,
        params={
            "dataset_label": label,
            "keep_archive": True,
            "extract_archive": extract_archive,
        },
    )
    response.raise_for_status()
    res_json = response.json()

    logger.info(res_json)
    return res_json


def _get_prediction_location(prediction, output_dir):
    with open(os.path.join(output_dir, "workflow_report.json"), "r") as report_file:
        report = json.load(report_file)

    return report[prediction.value]["location"].replace("file://", "")


@task
def gather_report(dag_info):
    workflow_fn = "predictions.cwl"
    params_fn = "params.json"
    metadata_fn = "metadata.yaml"

    dag_id, dag_run_id = dag_info["dag_id"], dag_info["dag_run_id"]
    output_dir = _get_output_dir(dag_id, dag_run_id)
    with open(os.path.join(output_dir, "workflow_report.json")) as f:
        airflow_report = json.load(f)
    report = {"workflow": workflow_fn, "params": params_fn, "outs": {}}

    dag_run = get_current_context()["dag_run"]
    task_predictions = dag_run.get_task_instance("predictions")
    report["start_date"] = task_predictions.start_date
    report["end_date"] = task_predictions.end_date

    workflow_keys = ["workflow_def", "workflow_params"]
    for key, value in airflow_report.items():
        if key not in workflow_keys:
            value["location"] = os.path.basename(value["location"])
            report["outs"][key] = value

    with open(os.path.join(output_dir, metadata_fn), "w") as report_file:
        yaml.dump(report, report_file)

    with open(os.path.join(output_dir, workflow_fn), "w") as cwl:
        yaml.dump(airflow_report["workflow_def"], cwl)

    with open(os.path.join(output_dir, params_fn), "w") as params:
        json.dump(airflow_report["workflow_params"], params)
    return output_dir


@task
def generate_rocrate(input_dir: str):
    command = f"-v {input_dir}:{input_dir} prov_crate  -o {input_dir}/rocrate {input_dir}".split()
    docker_run(command)


dag = create_dag()
