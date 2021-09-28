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


class OaiGnbCharm(OaiCharm):
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
            container_name="gnb",
            service_name="oai_gnb",
        )
        # Observe charm events
        event_observer_mapping = {
            self.on.gnb_pebble_ready: self._on_oai_gnb_pebble_ready,
            # self.on.stop: self._on_stop,
            self.on.config_changed: self._on_config_changed,
            self.on.gnb_relation_joined: self._on_gnb_relation_joined,
            self.on.gnb_relation_changed: self._on_gnb_relation_changed,
            self.on.amf_relation_changed: self._update_service,
            self.on.amf_relation_broken: self._update_service,
            self.on.spgwu_relation_changed: self._update_service,
            self.on.spgwu_relation_broken: self._update_service,
        }
        for event, observer in event_observer_mapping.items():
            self.framework.observe(event, observer)
        # Set defaults in Stored State for the relation data
        self._stored.set_default(
            amf_host=None,
            amf_port=None,
            amf_api_version=None,
            gnb_registered=False,
            ue_registered=False,
            spgwu_ready=False,
        )

    @property
    def imsi(self):
        return "208950000000031"

    @property
    def gnb_name(self):
        return f'gnb-rfsim-{self.unit.name.replace("/", "-")}'

    ####################################
    # Charm Events handlers
    ####################################

    def _on_oai_gnb_pebble_ready(self, event):
        pod_ip = self.pod_ip
        if not pod_ip:
            event.defer()
            return
        try:
            container = event.workload
            self._patch_gnb_id(container)
            self._add_oai_gnb_layer(container, pod_ip)
            self._update_service(event)
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()

    def _on_stop(self, event):
        pass

    def _on_config_changed(self, event):
        self.update_tcpdump_service(event)

    def _on_gnb_relation_joined(self, event):
        try:
            if self.unit.is_leader() and self._stored.gnb_registered:
                self._provide_service_info()
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()

    def _on_gnb_relation_changed(self, event):
        if self.unit.is_leader() and all(
            key in event.relation.data[event.app] for key in ("ue-imsi", "ue-status")
        ):
            ue_imsi = event.relation.data[event.app]["ue-imsi"]
            ue_status = event.relation.data[event.app]["ue-status"]
            relation = self.framework.model.get_relation("amf")
            relation.data[self.app]["ue-imsi"] = ue_imsi
            relation.data[self.app]["ue-status"] = ue_status

    def _update_service(self, event):
        try:
            logger.info("Updating service...")
            if not self.service_exists():
                logger.warning("service does not exist")
                return
            # Load data from dependent relations
            self._load_amf_data()
            self._load_spgwu_data()
            relations_ready = self.is_amf_ready and self.is_spgwu_ready
            if not relations_ready:
                self.unit.status = BlockedStatus("need amf and spgwu relations")
                if self.is_service_running():
                    self.stop_service()
            else:
                if not self.is_service_running():
                    self._configure_service()
                    self.start_service()
                    self._wait_until_service_is_active()
                relation = self.framework.model.get_relation("amf")
                if self._stored.gnb_registered:
                    if relation and self.unit in relation.data:
                        relation.data[self.unit]["gnb-status"] = "registered"
                else:
                    if relation and self.unit in relation.data:
                        relation.data[self.unit]["gnb-name"] = self.gnb_name
                        relation.data[self.unit]["gnb-status"] = "started"
            if self._stored.gnb_registered:
                if self.unit.is_leader():
                    self._provide_service_info()
                self.unit.status = ActiveStatus("registered")
            if self.unit.is_leader():
                if self._stored.ue_registered:
                    relation = self.framework.model.get_relation("gnb")
                    relation.data[self.app][self.imsi] = "registered"
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()

    ####################################
    # Utils - Services and configuration
    ####################################

    def _provide_service_info(self):
        if pod_ip := self.pod_ip:
            for relation in self.framework.model.relations["gnb"]:
                logger.debug(f"Found relation {relation.name} with id {relation.id}")
                relation.data[self.app]["host"] = str(pod_ip)
                logger.info(
                    f"Info provided in relation {relation.name} (id {relation.id})"
                )

    def _wait_until_service_is_active(self):
        logger.debug("Waiting for service to be active...")
        self.unit.status = WaitingStatus("Waiting for service to be active...")
        active = self.search_logs({"ALL RUs ready - ALL gNBs ready"}, wait=True)
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
        logger.debug("Loading amf data from relation")
        relation = self.framework.model.get_relation("amf")
        if relation and relation.app in relation.data:
            relation_data = relation.data[relation.app]
            self._stored.amf_host = relation_data.get("ip-address")
            self._stored.amf_port = relation_data.get("port")
            self._stored.amf_api_version = relation_data.get("api-version")
            self._stored.gnb_registered = (
                relation_data.get(self.gnb_name) == "registered"
            )
            self._stored.ue_registered = relation_data.get(self.imsi) == "registered"
            logger.info("amf data loaded")
        else:
            self._stored.amf_host = None
            self._stored.amf_port = None
            self._stored.amf_api_version = None
            self._stored.gnb_registered = False
            logger.warning("no relation found")

    @property
    def is_spgwu_ready(self):
        is_ready = self._stored.spgwu_ready
        logger.info(f'spgwu is{" " if is_ready else " not "}ready')
        return is_ready

    def _load_spgwu_data(self):
        logger.debug("Loading spgwu data from relation")
        relation = self.framework.model.get_relation("spgwu")
        if relation and relation.app in relation.data:
            relation_data = relation.data[relation.app]
            self._stored.spgwu_ready = relation_data.get("ready") == "True"
            logger.info("spgwu data loaded")
        else:
            self._stored.spgwu_ready = False
            logger.warning("no relation found")

    def _configure_service(self):
        if not self.service_exists():
            logger.debug("Cannot configure service: service does not exist yet")
            return
        logger.debug("Configuring gnb service")
        container = self.unit.get_container("gnb")
        container.add_layer(
            "oai_gnb",
            {
                "services": {
                    "oai_gnb": {
                        "override": "merge",
                        "environment": {
                            "AMF_IP_ADDRESS": self._stored.amf_host,
                        },
                    }
                },
            },
            combine=True,
        )
        logger.info("gnb service configured")

    def _patch_gnb_id(self, container):
        logger.debug("Patching GNB id...")
        gnb_id = self.unit.name[::-1].split("/")[0][::-1]
        gnb_sa_tdd_conf = (
            container.pull("/opt/oai-gnb/etc/gnb.sa.tdd.conf")
            .read()
            .replace("0xe00", f"0xe0{gnb_id}")
        )
        container.push("/opt/oai-gnb/etc/gnb.sa.tdd.conf", gnb_sa_tdd_conf)
        logger.info(f"GNB patched with id {gnb_id}")

    def _add_oai_gnb_layer(self, container, pod_ip):
        entrypoint = "/opt/oai-gnb/bin/entrypoint.sh"
        command = " ".join(
            ["/opt/oai-gnb/bin/nr-softmodem.Rel15", "-O", "/opt/oai-gnb/etc/gnb.conf"]
        )
        pebble_layer = {
            "summary": "oai_gnb layer",
            "description": "pebble config layer for oai_gnb",
            "services": {
                "oai_gnb": {
                    "override": "replace",
                    "summary": "oai_gnb",
                    "command": f"{entrypoint} {command}",
                    "environment": {
                        "TZ": "Europe/Paris",
                        "RFSIMULATOR": "server",
                        "USE_SA_TDD_MONO": "yes",
                        "GNB_NAME": self.gnb_name,
                        "MCC": "208",
                        "MNC": "95",
                        "MNC_LENGTH": "2",
                        "TAC": "1",
                        "NSSAI_SST": "1",
                        "NSSAI_SD0": "1",
                        "NSSAI_SD1": "112233",
                        "GNB_NGA_IF_NAME": "eth0",
                        "GNB_NGA_IP_ADDRESS": str(pod_ip),
                        "GNB_NGU_IF_NAME": "eth0",
                        "GNB_NGU_IP_ADDRESS": str(pod_ip),
                        "USE_ADDITIONAL_OPTIONS": "--sa -E --rfsim",
                    },
                }
            },
        }
        container.add_layer("oai_gnb", pebble_layer, combine=True)
        logger.info("oai_gnb layer added")


if __name__ == "__main__":
    main(OaiGnbCharm)
