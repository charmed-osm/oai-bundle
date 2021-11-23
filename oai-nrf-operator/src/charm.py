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

from utils import OaiCharm

logger = logging.getLogger(__name__)

HTTP1_PORT = 80
HTTP2_PORT = 9090


class OaiNrfCharm(OaiCharm):
    """Charm the service."""

    def __init__(self, *args):
        super().__init__(
            *args,
            tcpdump=True,
            ports=[
                ("http1", HTTP1_PORT, HTTP1_PORT, "TCP"),
                ("http2", HTTP2_PORT, HTTP2_PORT, "TCP"),
            ],
            privileged=True,
            container_name="nrf",
            service_name="oai_nrf",
        )
        # Observe charm events
        event_observer_mapping = {
            self.on.nrf_pebble_ready: self._on_oai_nrf_pebble_ready,
            # self.on.stop: self._on_stop,
            self.on.config_changed: self._on_config_changed,
            self.on.nrf_relation_joined: self._on_nrf_relation_joined,
        }
        for event, observer in event_observer_mapping.items():
            self.framework.observe(event, observer)

    ####################################
    # Charm Events handlers
    ####################################

    def _on_oai_nrf_pebble_ready(self, event):
        try:
            container = event.workload
            self._add_oai_nrf_layer(container)
            self._update_service(event)
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()

    def _on_stop(self, event):
        pass

    def _on_config_changed(self, event):
        self.update_tcpdump_service(event)

    def _on_nrf_relation_joined(self, event):
        try:
            if self.is_service_running() and self.unit.is_leader():
                self._provide_service_info()
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()

    def _update_service(self, event):
        try:
            if self.service_exists() and not self.is_service_running():
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
        for relation in self.framework.model.relations["nrf"]:
            logger.debug(f"Found relation {relation.name} with id {relation.id}")
            relation.data[self.app]["host"] = self.app.name
            relation.data[self.app]["port"] = str(HTTP1_PORT)
            relation.data[self.app]["api-version"] = "v1"
            logger.info(f"Info provided in relation {relation.name} (id {relation.id})")

    def _wait_until_service_is_active(self):
        logger.debug("Waiting for service to be active")
        self.unit.status = WaitingStatus("Waiting for service to be active...")
        active = self.search_logs({"[info ] HTTP1 server started"}, wait=True)
        if active:
            # wait extra time
            time.sleep(10)
            self.unit.status = ActiveStatus()
        else:
            self.unit.status = BlockedStatus("service couldn't start")

    def _add_oai_nrf_layer(self, container):
        entrypoint = "/bin/bash /openair-nrf/bin/entrypoint.sh"
        command = " ".join(
            ["/openair-nrf/bin/oai_nrf", "-c", "/openair-nrf/etc/nrf.conf", "-o"]
        )
        pebble_layer = {
            "summary": "oai_nrf layer",
            "description": "pebble config layer for oai_nrf",
            "services": {
                "oai_nrf": {
                    "override": "replace",
                    "summary": "oai_nrf",
                    "command": f"{entrypoint} {command}",
                    "environment": {
                        "DEBIAN_FRONTEND": "noninteractive",
                        "TZ": "Europe/Paris",
                        "INSTANCE": "0",
                        "PID_DIRECTORY": "/var/run",
                        "NRF_INTERFACE_NAME_FOR_SBI": self.config['nrf-interface-name-for-sbi'],
                        "NRF_INTERFACE_PORT_FOR_SBI": self.config['nrf-interface-port-for-sbi'],
                        "NRF_INTERFACE_HTTP2_PORT_FOR_SBI": self.config['nrf-interface-http2-port-for-sbi'],
                        "NRF_API_VERSION": self.config['nrf-api-version'],
                    },
                }
            },
        }
        container.add_layer("oai_nrf", pebble_layer, combine=True)
        logger.info("oai_nrf layer added")


if __name__ == "__main__":
    main(OaiNrfCharm, use_juju_for_storage=True)
