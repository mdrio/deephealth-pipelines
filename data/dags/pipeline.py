#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
import os
from datetime import datetime

import requests
from airflow.decorators import task
from airflow.models import Variable
from airflow.utils.task_group import TaskGroup
from libs.tasks import (
    Prediction,
    add_prediction_to_omero,
    add_prediction_to_promort,
    add_slide_to_omero,
    add_slide_to_promort,
    gather_report,
    generate_rocrate,
    move_slide,
    predictions,
    prepare_data,
    tissue_branch,
    tumor_branch,
)

from airflow import DAG

logger = logging.getLogger("watch-dir")

OME_SEADRAGON_URL = Variable.get("OME_SEADRAGON_URL")
STAGE_DIR = Variable.get("STAGE_DIR")
FAILED_DIR = Variable.get("FAILED_DIR")


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


dag = create_dag()
