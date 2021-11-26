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

SPGWU_PORT = 8805
S1U_PORT = 2152
IPERF = 5001


class OaiSpgwuTinyCharm(OaiCharm):
    """Charm the service."""

    def __init__(self, *args):
        super().__init__(
            *args,
            tcpdump=True,
            ports=[
                ("oai-spgwu-tiny", SPGWU_PORT, SPGWU_PORT, "UDP"),
                ("s1u", S1U_PORT, S1U_PORT, "UDP"),
                ("iperf", IPERF, IPERF, "UDP"),
            ],
            privileged=True,
            container_name="spgwu-tiny",
            service_name="oai_spgwu_tiny",
        )
        # Observe charm events
        event_observer_mapping = {
            self.on.spgwu_tiny_pebble_ready: self._on_oai_spgwu_tiny_pebble_ready,
            # self.on.stop: self._on_stop,
            self.on.config_changed: self._on_config_changed,
            self.on.spgwu_relation_joined: self._on_spgwu_relation_joined,
            self.on.nrf_relation_changed: self._update_service,
            self.on.nrf_relation_broken: self._update_service,
            self.on.smf_relation_changed: self._update_service,
            self.on.smf_relation_broken: self._update_service,
        }
        for event, observer in event_observer_mapping.items():
            self.framework.observe(event, observer)
        # Set defaults in Stored State for the relation data
        self._stored.set_default(
            nrf_host=None,
            nrf_port=None,
            nrf_api_version=None,
            smf_ready=False,
        )

    ####################################
    # Charm Events handlers
    ####################################

    def _on_oai_spgwu_tiny_pebble_ready(self, event):
        try:
            container = event.workload
            self._add_oai_spgwu_tiny_layer(container)
            self._update_service(event)
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()

    def _on_stop(self, event):
        pass

    def _on_config_changed(self, event):
        self.update_tcpdump_service(event)

    def _on_spgwu_relation_joined(self, event):
        try:
            if self.unit.is_leader() and self.is_service_running():
                self._provide_service_info()
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()

    def _update_service(self, event):
        try:
            logger.info("Updating service...")
            if not self.service_exists():
                logger.warning("service does not exist")
                return
            # Load data from dependent relations
            self._load_nrf_data()
            self._load_smf_data()
            relations_ready = self.is_nrf_ready and self.is_smf_ready
            if not relations_ready:
                self.unit.status = BlockedStatus("need nrf and smf relations")
                if self.is_service_running():
                    self.stop_service()
            elif not self.is_service_running():
                self._configure_service()
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
        for relation in self.framework.model.relations["spgwu"]:
            logger.debug(f"Found relation {relation.name} with id {relation.id}")
            relation.data[self.app]["ready"] = str(True)
            logger.info(f"Info provided in relation {relation.name} (id {relation.id})")

    def _wait_until_service_is_active(self):
        logger.debug("Waiting for service to be active...")
        self.unit.status = WaitingStatus("Waiting for service to be active...")
        active = self.search_logs(
            {"[spgwu_app] [start] Started", "Got successful response from NRF"},
            wait=True,
        )
        if active:
            # wait extra time
            time.sleep(10)
            self.unit.status = ActiveStatus()
        else:
            self.unit.status = BlockedStatus("service couldn't start")

    @property
    def is_nrf_ready(self):
        is_ready = (
            self._stored.nrf_host
            and self._stored.nrf_port
            and self._stored.nrf_api_version
        )
        logger.info(f'nrf is{" " if is_ready else " not "}ready')
        return is_ready

    def _load_nrf_data(self):
        logger.debug("Loading nrf data from relation")
        relation = self.framework.model.get_relation("nrf")
        if relation and relation.app in relation.data:
            relation_data = relation.data[relation.app]
            self._stored.nrf_host = relation_data.get("host")
            self._stored.nrf_port = relation_data.get("port")
            self._stored.nrf_api_version = relation_data.get("api-version")
            logger.info("nrf data loaded")
        else:
            self._stored.nrf_host = None
            self._stored.nrf_port = None
            self._stored.nrf_api_version = None
            logger.warning("no relation found")

    @property
    def is_smf_ready(self):
        is_ready = self._stored.smf_ready
        logger.info(f'smf is{" " if is_ready else " not "}ready')
        return is_ready

    def _load_smf_data(self):
        logger.debug("Loading smf data from relation")
        relation = self.framework.model.get_relation("smf")
        if relation and relation.app in relation.data:
            relation_data = relation.data[relation.app]
            self._stored.smf_ready = relation_data.get("ready") == "True"
            logger.info("smf data loaded")
        else:
            self._stored.smf_ready = False
            logger.warning("no relation found")

    def _configure_service(self):
        if not self.service_exists():
            logger.debug("Cannot configure service: service does not exist yet")
            return
        logger.debug("Configuring spgwu service")
        container = self.unit.get_container("spgwu-tiny")
        if self.service_name in container.get_plan().services:
            container.add_layer(
                "oai_spgwu_tiny",
                {
                    "services": {
                        "oai_spgwu_tiny": {
                            "override": "merge",
                            "environment": {
                                "REGISTER_NRF": "yes",
                                "USE_FQDN_NRF": "yes",
                                "NRF_FQDN": self._stored.nrf_host,
                                "NRF_IPV4_ADDRESS": "127.0.0.1",
                                "NRF_PORT": self._stored.nrf_port,
                                "NRF_API_VERSION": self._stored.nrf_api_version,
                            },
                        }
                    },
                },
                combine=True,
            )
        logger.info("spgwu service configured")

    def _add_oai_spgwu_tiny_layer(self, container):
        entrypoint = "/bin/bash /openair-spgwu-tiny/bin/entrypoint.sh"
        command = " ".join(
            [
                "/openair-spgwu-tiny/bin/oai_spgwu",
                "-c",
                "/openair-spgwu-tiny/etc/spgw_u.conf",
                "-o",
            ]
        )
        pebble_layer = {
            "summary": "oai_spgwu_tiny layer",
            "description": "pebble config layer for oai_spgwu_tiny",
            "services": {
                "oai_spgwu_tiny": {
                    "override": "replace",
                    "summary": "oai_spgwu_tiny",
                    "command": f"{entrypoint} {command}",
                    "environment": {
                        "DEBIAN_FRONTEND": "noninteractive",
                        "TZ": "Europe/Paris",
                        "GW_ID": self.config['gw-id'],
                        "MCC": self.config['mcc'],
                        "MNC03": self.config['mnc03'],
                        "REALM": self.config['realm'],
                        "PID_DIRECTORY": "/var/run",
                        "SGW_INTERFACE_NAME_FOR_S1U_S12_S4_UP": self.config['sgw-interface-name-for-s1u-s12-s4-up'],
                        "THREAD_S1U_PRIO": self.config['thread-s1u-prio'],
                        "S1U_THREADS": self.config['s1u-threads'],
                        "SGW_INTERFACE_NAME_FOR_SX": self.config['sgw-interface-name-for-sx'],
                        "THREAD_SX_PRIO": self.config['thread-sx-prio'],
                        "SX_THREADS": self.config['sx-threads'],
                        "PGW_INTERFACE_NAME_FOR_SGI": self.config['pgw-interface-name-for-sgi'],
                        "THREAD_SGI_PRIO": self.config['thread-sgi-prio'],
                        "SGI_THREADS": self.config['sgi-threads'],
                        "NETWORK_UE_NAT_OPTION": self.config['network-ue-nat-option'],
                        "GTP_EXTENSION_HEADER_PRESENT": self.config['gtp-extension-header-present'],
                        "NETWORK_UE_IP": self.config['network-ue-ip'],
                        "SPGWC0_IP_ADDRESS": self.config['spgwc0-ip-address'],
                        "BYPASS_UL_PFCP_RULES": self.config['bypass-ul-pfcp-rules'],
                        "ENABLE_5G_FEATURES": self.config['enable-5g-features'],
                        "NSSAI_SST_0": self.config['nssai-sst-0'],
                        "NSSAI_SD_0": self.config['nssa-sd-0'],
                        "DNN_0": self.config['dnn-0'],
                        "UPF_FQDN_5G": self.app.name,
                    },
                }
            },
        }
        container.add_layer("oai_spgwu_tiny", pebble_layer, combine=True)
        logger.info("oai_spgwu_tiny layer added")


if __name__ == "__main__":
    main(OaiSpgwuTinyCharm, use_juju_for_storage=True)
