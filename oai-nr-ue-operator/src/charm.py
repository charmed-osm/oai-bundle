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
import time

from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.pebble import ConnectionError

from utils import OaiCharm

logger = logging.getLogger(__name__)

S1C_PORT = 36412
S1U_PORT = 2152
X2C_PORT = 36422


class OaiNrUeCharm(OaiCharm):
    """Charm the service."""

    def __init__(self, *args):
        super().__init__(
            *args,
            tcpdump=True,
            ports=[
                ("s1c", S1C_PORT, S1C_PORT, "UDP"),
                ("s1u", S1U_PORT, S1U_PORT, "UDP"),
                ("x2c", X2C_PORT, X2C_PORT, "UDP"),
            ],
            privileged=True,
            container_name="nr-ue",
            service_name="oai_nr_ue",
        )
        # Observe charm events
        event_observer_mapping = {
            self.on.nr_ue_pebble_ready: self._on_oai_nr_ue_pebble_ready,
            # self.on.stop: self._on_stop,
            self.on.config_changed: self._on_config_changed,
            self.on.gnb_relation_changed: self._update_service,
            self.on.gnb_relation_broken: self._update_service,
            self.on.start_action: self._on_start_action,
            self.on.stop_action: self._on_stop_action,
        }
        for event, observer in event_observer_mapping.items():
            self.framework.observe(event, observer)
        # Set defaults in Stored State for the relation data
        self._stored.set_default(
            gnb_host=None,
            gnb_port=None,
            gnb_api_version=None,
            ue_registered=False,
        )

    @property
    def imsi(self):
        return "208950000000031"

    ####################################
    # Charm Events handlers
    ####################################

    def _on_oai_nr_ue_pebble_ready(self, event):
        try:
            container = event.workload
            self._add_oai_nr_ue_layer(container)
            self._update_service(event)
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()

    def _on_stop(self, event):
        pass

    def _on_config_changed(self, event):
        self.update_tcpdump_service(event)

    def _update_service(self, event):
        try:
            if not self.unit.is_leader():
                logger.warning("HA is not supported")
                self.unit.status = BlockedStatus("HA is not supported")
                return
            logger.info("Updating service...")
            if not self.service_exists():
                logger.warning("service does not exist")
                return
            # Load data from dependent relations
            self._load_gnb_data()
            relations_ready = self.is_gnb_ready
            if not relations_ready:
                self.unit.status = BlockedStatus("need gnb relation")
                if self.is_service_running():
                    self.stop_service()
                return
            else:
                if not self.is_service_running():
                    self._configure_service()
                    time.sleep(10)
                    self._backup_conf_files()
                    self.start_service()
                relation = self.framework.model.get_relation("gnb")
                if self._stored.ue_registered:
                    if relation and self.app in relation.data:
                        relation.data[self.app]["ue-status"] = "registered"
                else:
                    if relation and self.app in relation.data:
                        relation.data[self.app]["ue-imsi"] = self.imsi
                        relation.data[self.app]["ue-status"] = "started"
            if self._stored.ue_registered:
                self.unit.status = ActiveStatus("registered")
            else:
                self.unit.status = WaitingStatus("waiting for registration")
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()

    def _on_start_action(self, event):
        try:
            if not self.service_exists():
                event.set_results({"output": "service does not exist yet"})
                return
            if not self.is_service_running():
                self.start_service()
                event.set_results({"output": "service has been started successfully"})
            else:
                event.set_results({"output": "service is already running"})
        except Exception as e:
            event.fail(f"Action failed. Reason: {e}")

    def _on_stop_action(self, event):
        try:
            if not self.service_exists():
                event.set_results({"output": "service does not exist yet"})
                return
            if self.is_service_running():
                self.stop_service()
                self._restore_conf_files()
                event.set_results({"output": "service has been stopped successfully"})
            else:
                event.set_results({"output": "service is already stopped"})
        except Exception as e:
            event.fail(f"Action failed. Reason: {e}")

    ####################################
    # Utils - Services and configuration
    ####################################

    @property
    def is_gnb_ready(self):
        is_ready = self._stored.gnb_host
        logger.info(f'gnb is{" " if is_ready else " not "}ready')
        return is_ready

    def _load_gnb_data(self):
        logger.debug("Loading gnb data from relation")
        relation = self.framework.model.get_relation("gnb")
        if relation and relation.app in relation.data:
            self._stored.gnb_host = relation.data[relation.app].get("host")
            self._stored.ue_registered = (
                relation.data[relation.app].get(self.imsi) == "registered"
            )
            logger.info("gnb data loaded")
        else:
            self._stored.gnb_host = None
            self._stored.ue_registered = False
            logger.warning("no relation found")

    def _configure_service(self):
        if not self.service_exists():
            logger.debug("Cannot configure service: service does not exist yet")
            return
        logger.debug("Configuring nr-ue service")
        container = self.unit.get_container("nr-ue")
        container.add_layer(
            "oai_nr_ue",
            {
                "services": {
                    "oai_nr_ue": {
                        "override": "merge",
                        "environment": {
                            "RFSIMULATOR": self._stored.gnb_host,
                        },
                    }
                },
            },
            combine=True,
        )
        logger.info("nr-ue service configured")

    def _add_oai_nr_ue_layer(self, container):
        entrypoint = "/opt/oai-nr-ue/bin/entrypoint.sh"
        command = " ".join(
            [
                "/opt/oai-nr-ue/bin/nr-uesoftmodem.Rel15",
                "-O",
                "/opt/oai-nr-ue/etc/nr-ue-sim.conf",
            ]
        )
        pebble_layer = {
            "summary": "oai_nr_ue layer",
            "description": "pebble config layer for oai_nr_ue",
            "services": {
                "oai_nr_ue": {
                    "override": "replace",
                    "summary": "oai_nr_ue",
                    "command": f"{entrypoint} {command}",
                    "environment": {
                        "TZ": "Europe/Paris",
                        "FULL_IMSI": self.imsi,
                        "FULL_KEY": "0C0A34601D4F07677303652C0462535B",
                        "OPC": "63bfa50ee6523365ff14c1f45f88737d",
                        "DNN": "oai",
                        "NSSAI_SST": "1",
                        "NSSAI_SD": "1",
                        "USE_ADDITIONAL_OPTIONS": "-E --sa --rfsim -r 106 --numerology 1 -C 3619200000 --nokrnmod",
                    },
                }
            },
        }
        container.add_layer("oai_nr_ue", pebble_layer, combine=True)
        logger.info("oai_nr_ue layer added")

    def _backup_conf_files(self):
        container = self.unit.get_container("nr-ue")
        root_folder = "/opt/oai-nr-ue"
        files = {f"{root_folder}/etc/nr-ue-sim.conf"}
        for file in files:
            file_content = container.pull(file).read()
            container.push(f"{file}_bkp", file_content)

    def _restore_conf_files(self):
        container = self.unit.get_container("nr-ue")
        root_folder = "/opt/oai-nr-ue"
        files = {f"{root_folder}/etc/nr-ue-sim.conf"}
        for file in files:
            file_content = container.pull(f"{file}_bkp").read()
            container.push(file, file_content)

if __name__ == "__main__":
    main(OaiNrUeCharm)
