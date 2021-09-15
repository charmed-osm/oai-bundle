#!/usr/bin/env python3
# Copyright 2021 David Garcia
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service.

Refer to the following post for a quick-start guide that will help you
develop a new k8s charm using the Operator Framework:

    https://discourse.charmhub.io/t/4208
"""

from ipaddress import IPv4Address
import logging
from pathlib import Path
from subprocess import check_output
import time
from typing import Optional

from kubernetes import kubernetes
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus
from ops.pebble import ConnectionError

from kubernetes_service import K8sServicePatch, PatchFailed

logger = logging.getLogger(__name__)

MYSQL_PORT = 3306


class OaiDbCharm(CharmBase):
    """Charm the service."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        event_observer_mapping = {
            self.on.install: self._on_install,
            self.on.db_pebble_ready: self._on_oai_db_pebble_ready,
            self.on.db_relation_joined: self._provide_service_info,
        }
        for event, observer in event_observer_mapping.items():
            self.framework.observe(event, observer)
        self._stored.set_default(
            _k8s_stateful_patched=False,
            _k8s_authed=False,
        )

    ####################################
    # Observers - Relation Events
    ####################################

    def _provide_service_info(self, event):
        if self.unit.is_leader() and self.is_service_running:
            for relation in self.framework.model.relations["db"]:
                relation.data[self.app]["host"] = self.app.name
                relation.data[self.app]["port"] = str(MYSQL_PORT)
                relation.data[self.app]["user"] = "root"
                relation.data[self.app]["password"] = "root"
                relation.data[self.app]["database"] = "oai_db"
            else:
                logger.info("not relations found")

    ####################################
    # Observers - Charm Events
    ####################################
    def _on_install(self, event):
        self._k8s_auth()
        K8sServicePatch.set_ports(self.app.name, [("myqsl", 3306, 3306, "TCP")])

    ####################################
    # Observers - Pebble Events
    ####################################

    def _on_oai_db_pebble_ready(self, event):
        container = event.workload
        pebble_layer = {
            "summary": "oai_db layer",
            "description": "pebble config layer for oai_db",
            "services": {
                "oai_db": {
                    "override": "replace",
                    "summary": "oai_db",
                    "command": "docker-entrypoint.sh mysqld",
                    "environment": {
                        "MYSQL_ROOT_PASSWORD": "root",
                        "MYSQL_DATABASE": "oai_db",
                        "GOSU_VERSION": "1.13",
                        "MARIADB_MAJOR": "10.3",
                        "MARIADB_VERSION": "1:10.3.31+maria~focal",
                    },
                }
            },
        }
        try:
            container.add_layer("oai_db", pebble_layer, combine=True)
            self._update_service(event)
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()
            return

    ####################################
    # Properties
    ####################################

    @property
    def pod_ip(self) -> Optional[IPv4Address]:
        return IPv4Address(
            check_output(["unit-get", "private-address"]).decode().strip()
        )

    @property
    def container_name(self):
        return "db"

    @property
    def service_name(self):
        return "oai_db"

    @property
    def is_service_running(self):
        container = self.unit.get_container(self.container_name)
        return (
            self.service_name in container.get_plan().services
            and container.get_service(self.service_name).is_running()
        )

    ####################################
    # Utils - Services and configuration
    ####################################

    def _update_service(self, event):
        self._initialize_db()
        if self._start_service(container_name="db", service_name="oai_db"):
            self.unit.status = WaitingStatus(
                "waiting 30 seconds for the service to start"
            )
            time.sleep(30)
            self._provide_service_info(event)
            self.unit.status = ActiveStatus()

    def _initialize_db(self):
        container = self.unit.get_container("db")
        container.push(
            "/docker-entrypoint-initdb.d/db.sql", Path("templates/db.sql").read_text()
        )

    def _start_service(self, container_name, service_name):
        container = self.unit.get_container(container_name)
        service_exists = service_name in container.get_plan().services
        is_running = (
            container.get_service(service_name).is_running()
            if service_exists
            else False
        )

        if service_exists and not is_running:
            logger.info(f"{container.get_plan()}")
            container.start(service_name)
            return True

    def _stop_service(self, container_name, service_name):
        container = self.unit.get_container(container_name)
        is_running = (
            service_name in container.get_plan().services
            and container.get_service(service_name).is_running()
        )
        if is_running:
            container.stop(service_name)

    ####################################
    # Utils - K8s authentication
    ###########################

    def _k8s_auth(self) -> bool:
        """Authenticate to kubernetes."""
        if self._stored._k8s_authed:
            return True
        kubernetes.config.load_incluster_config()
        self._stored._k8s_authed = True


if __name__ == "__main__":
    main(OaiDbCharm, use_juju_for_storage=True)
