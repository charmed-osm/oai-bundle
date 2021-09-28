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

import logging
from pathlib import Path
import time

from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.pebble import ConnectionError

from utils import OaiCharm

logger = logging.getLogger(__name__)

MYSQL_PORT = 3306


class OaiDbCharm(OaiCharm):
    """Charm the service."""

    def __init__(self, *args):
        super().__init__(
            *args,
            ports=[("myqsl", MYSQL_PORT, MYSQL_PORT, "TCP")],
            container_name="db",
            service_name="oai_db",
        )
        # Observe charm events
        event_observer_mapping = {
            # self.on.stop: self._on_stop,
            self.on.db_pebble_ready: self._on_oai_db_pebble_ready,
            self.on.db_relation_joined: self._on_db_relation_joined,
        }
        for event, observer in event_observer_mapping.items():
            self.framework.observe(event, observer)

    ####################################
    # Charm Events handlers
    ####################################

    def _on_oai_db_pebble_ready(self, event):
        try:
            container = event.workload
            self._add_oai_db_layer(container)
            self._update_service(event)
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()

    # def _on_stop(self, event):
    #     if self.unit.is_leader():
    #         self._clear_service_info()

    def _on_db_relation_joined(self, event):
        try:
            if self.is_service_running() and self.unit.is_leader():
                self._provide_service_info()
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()

    def _update_service(self, event):
        try:
            if self.service_exists() and not self.is_service_running():
                self._initialize_db()
                self.start_service()
                self._wait_until_service_is_active()
                if self.unit.is_leader():
                    self._provide_service_info()
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()

    ####################################
    # Utils - Services and configuration
    ####################################

    def _provide_service_info(self):
        for relation in self.framework.model.relations["db"]:
            logger.debug(f"Found relation {relation.name} with id {relation.id}")
            relation.data[self.app]["host"] = self.app.name
            relation.data[self.app]["port"] = str(MYSQL_PORT)
            relation.data[self.app]["user"] = "root"
            relation.data[self.app]["password"] = "root"
            relation.data[self.app]["database"] = "oai_db"
            logger.info(f"Info provided in relation {relation.name} (id {relation.id})")

    def _clear_service_info(self):
        for relation in self.framework.model.relations["db"]:
            logger.debug(f"Found relation {relation.name} with id {relation.id}")
            relation.data[self.app]["host"] = ""
            relation.data[self.app]["port"] = ""
            relation.data[self.app]["user"] = ""
            relation.data[self.app]["password"] = ""
            relation.data[self.app]["database"] = ""
            logger.info(f"Info cleared in relation {relation.name} (id {relation.id})")

    def _wait_until_service_is_active(self):
        logger.debug("Waiting for service to be active")
        self.unit.status = WaitingStatus("Waiting for service to be active...")
        active = self.search_logs({"[Note] mysqld: ready for connections."}, wait=True)
        if active:
            # wait extra time
            time.sleep(10)
            self.unit.status = ActiveStatus()
        else:
            self.unit.status = BlockedStatus("service couldn't start")

    def _initialize_db(self):
        try:
            logger.debug("Initializing DB")
            container = self.unit.get_container("db")
            db_sql_data = Path("templates/db.sql").read_text()
            container.push("/docker-entrypoint-initdb.d/db.sql", db_sql_data)
            logger.info("DB has been successfully initialized")
        except Exception as e:
            logger.error(f"failed initializing the DB: {e}")

    def _add_oai_db_layer(self, container):
        container.add_layer(
            "oai_db",
            {
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
            },
            combine=True,
        )
        logger.info("oai_db layer added")


if __name__ == "__main__":
    main(OaiDbCharm, use_juju_for_storage=True)
