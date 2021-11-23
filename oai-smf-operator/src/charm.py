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

SMF_PORT = 8805
HTTP1_PORT = 80
HTTP2_PORT = 9090


class OaiSmfCharm(OaiCharm):
    """Charm the service."""

    def __init__(self, *args):
        super().__init__(
            *args,
            tcpdump=True,
            ports=[
                ("oai-smf", SMF_PORT, SMF_PORT, "UDP"),
                ("http1", HTTP1_PORT, HTTP1_PORT, "TCP"),
                ("http2", HTTP2_PORT, HTTP2_PORT, "TCP"),
            ],
            privileged=True,
            container_name="smf",
            service_name="oai_smf",
        )
        # Observe charm events
        event_observer_mapping = {
            self.on.smf_pebble_ready: self._on_oai_smf_pebble_ready,
            # self.on.stop: self._on_stop,
            self.on.config_changed: self._on_config_changed,
            self.on.smf_relation_joined: self._on_smf_relation_joined,
            self.on.amf_relation_changed: self._update_service,
            self.on.amf_relation_broken: self._update_service,
            self.on.nrf_relation_changed: self._update_service,
            self.on.nrf_relation_broken: self._update_service,
        }
        for event, observer in event_observer_mapping.items():
            self.framework.observe(event, observer)
        # Set defaults in Stored State for the relation data
        self._stored.set_default(
            amf_host=None,
            amf_port=None,
            amf_api_version=None,
            nrf_host=None,
            nrf_port=None,
            nrf_api_version=None,
        )

    ####################################
    # Charm events handlers
    ####################################

    def _on_oai_smf_pebble_ready(self, event):
        try:
            container = event.workload
            self._add_oai_smf_layer(container)
            self._update_service(event)
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()

    def _on_stop(self, event):
        pass

    def _on_config_changed(self, event):
        self.update_tcpdump_service(event)

    def _on_smf_relation_joined(self, event):
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
            self._load_amf_data()
            self._load_nrf_data()
            relations_ready = self.is_nrf_ready and self.is_amf_ready
            if not relations_ready:
                self.unit.status = BlockedStatus("need nrf and amf relations")
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
        for relation in self.framework.model.relations["smf"]:
            logger.debug(f"Found relation {relation.name} with id {relation.id}")
            relation.data[self.app]["ready"] = str(True)
            logger.info(f"Info provided in relation {relation.name} (id {relation.id})")

    def _wait_until_service_is_active(self):
        logger.debug("Waiting for service to be active...")
        self.unit.status = WaitingStatus("Waiting for service to be active...")
        active = self.search_logs(
            {
                "[smf_sbi] [start] Started",
                "[smf_app] [start] Started",
                "[sbi_srv] [info ] HTTP1 server started",
                "[sbi_srv] [info ] HTTP2 server started",
            },
            wait=True,
        )
        if active:
            # wait extra time
            time.sleep(10)
            self.unit.status = ActiveStatus()
        else:
            self.unit.status = BlockedStatus("service couldn't start")

    @property
    def is_amf_ready(self):
        is_ready = (
            self._stored.amf_host
            and self._stored.amf_port
            and self._stored.amf_api_version
        )
        logger.info(f'amf is{" " if is_ready else " not "}ready')
        return is_ready

    def _load_amf_data(self):
        logger.debug("Loading nrf data from relation")
        relation = self.framework.model.get_relation("amf")
        if relation and relation.app in relation.data:
            relation_data = relation.data[relation.app]
            self._stored.amf_host = relation_data.get("host")
            self._stored.amf_port = relation_data.get("port")
            self._stored.amf_api_version = relation_data.get("api-version")
            logger.info("amf data loaded")
        else:
            self._stored.amf_host = None
            self._stored.amf_port = None
            self._stored.amf_api_version = None
            logger.warning("no relation found")

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

    def _configure_service(self):
        if not self.service_exists():
            logger.debug("Cannot configure service: service does not exist yet")
            return
        logger.debug("Configuring smf service")
        container = self.unit.get_container("smf")
        container.add_layer(
            "oai_smf",
            {
                "services": {
                    "oai_smf": {
                        "override": "merge",
                        "environment": {
                            "NRF_FQDN": self._stored.nrf_host,
                            "NRF_IPV4_ADDRESS": "127.0.0.1",
                            "NRF_PORT": self._stored.nrf_port,
                            "NRF_API_VERSION": self._stored.nrf_api_version,
                            "AMF_IPV4_ADDRESS": "127.0.0.1",
                            "AMF_PORT": self._stored.amf_port,
                            "AMF_API_VERSION": self._stored.amf_api_version,
                            "AMF_FQDN": self._stored.amf_host,
                        },
                    }
                },
            },
            combine=True,
        )
        logger.info("smf service configured")

    def _add_oai_smf_layer(self, container):
        entrypoint = "/bin/bash /openair-smf/bin/entrypoint.sh"
        command = " ".join(
            ["/openair-smf/bin/oai_smf", "-c", "/openair-smf/etc/smf.conf", "-o"]
        )
        pebble_layer = {
            "summary": "oai_smf layer",
            "description": "pebble config layer for oai_smf",
            "services": {
                "oai_smf": {
                    "override": "replace",
                    "summary": "oai_smf",
                    "command": f"{entrypoint} {command}",
                    "environment": {
                        "DEBIAN_FRONTEND": "noninteractive",
                        "TZ": "Europe/Paris",
                        "INSTANCE": "0",
                        "PID_DIRECTORY": "/var/run",
                        "SMF_INTERFACE_NAME_FOR_N4": self.config['smf_interface_name_for_n4'],
                        "SMF_INTERFACE_NAME_FOR_SBI": self.config['smf_interface_name_for_sbi'],
                        "SMF_INTERFACE_PORT_FOR_SBI": self.config['smf_interface_port_for_sbi'],
                        "SMF_INTERFACE_HTTP2_PORT_FOR_SBI": self.config['smf-interface-http2-port-for-sbi'],
                        "SMF_API_VERSION": self.config['smf_api_version'],
                        "DEFAULT_DNS_IPV4_ADDRESS": self.config['default-dns-ipv4-address'],
                        "DEFAULT_DNS_SEC_IPV4_ADDRESS": self.config['default-dns-sec-ipv4-address'],
                        "REGISTER_NRF": self.config['register-nrf'],
                        "DISCOVER_UPF": self.config['discover-upf'],
                        "USE_FQDN_DNS": self.config['use-fqdn-dns'],
                        "UDM_IPV4_ADDRESS": self.config['udm-ipv4-address'],
                        "UDM_PORT": self.config['udm-port'],
                        "UDM_API_VERSION": self.config['udm-api-version'],
                        "UDM_FQDN": self.config['udm-fqdn'],
                        "UPF_IPV4_ADDRESS": self.config['upf-ipv4-address'],
                        "UPF_FQDN_0": self.config['upf-fqdn-0'],
                    },
                }
            },
        }
        container.add_layer("oai_smf", pebble_layer, combine=True)
        logger.info("oai_smf layer added")


if __name__ == "__main__":
    main(OaiSmfCharm)
