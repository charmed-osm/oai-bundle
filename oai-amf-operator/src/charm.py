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

SCTP_PORT = 38412
HTTP1_PORT = 80
HTTP2_PORT = 9090


class OaiAmfCharm(OaiCharm):
    """Charm the service."""

    def __init__(self, *args):
        super().__init__(
            *args,
            tcpdump=True,
            ports=[
                ("oai-amf", SCTP_PORT, SCTP_PORT, "SCTP"),
                ("http1", HTTP1_PORT, HTTP1_PORT, "TCP"),
                ("http2", HTTP2_PORT, HTTP2_PORT, "TCP"),
            ],
            privileged=True,
            container_name="amf",
            service_name="oai_amf",
        )
        # Observe charm events
        event_observer_mapping = {
            self.on.amf_pebble_ready: self._on_oai_amf_pebble_ready,
            # self.on.stop: self._on_stop,
            self.on.config_changed: self._on_config_changed,
            self.on.amf_relation_joined: self._on_amf_relation_joined,
            self.on.amf_relation_changed: self._on_amf_relation_changed,
            self.on.nrf_relation_changed: self._update_service,
            self.on.nrf_relation_broken: self._update_service,
            self.on.db_relation_changed: self._update_service,
            self.on.db_relation_broken: self._update_service,
        }
        for event, observer in event_observer_mapping.items():
            self.framework.observe(event, observer)
        # Set defaults in Stored State for the relation data
        self._stored.set_default(
            nrf_host=None,
            nrf_port=None,
            nrf_api_version=None,
            db_host=None,
            db_port=None,
            db_user=None,
            db_password=None,
            db_database=None,
        )

    ####################################
    # Charm events handlers
    ####################################

    def _on_oai_amf_pebble_ready(self, event):
        try:
            container = event.workload
            self._add_oai_amf_layer(container)
            self._update_service(event)
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()

    # def _on_stop(self, event):
    #     if self.unit.is_leader():
    #         self._clear_service_info()

    def _on_config_changed(self, event):
        self.update_tcpdump_service(event)

    def _on_amf_relation_joined(self, event):
        try:
            if self.unit.is_leader() and self.is_service_running():
                self._provide_service_info()
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()

    def _on_amf_relation_changed(self, event):
        if event.unit in event.relation.data and all(
            key in event.relation.data[event.unit] for key in ("gnb-name", "gnb-status")
        ):
            gnb_name = event.relation.data[event.unit]["gnb-name"]
            gnb_status = event.relation.data[event.unit]["gnb-status"]
            if gnb_status == "started":
                self._wait_gnb_is_registered(gnb_name)
                event.relation.data[self.app][gnb_name] = "registered"
        if event.app in event.relation.data and all(
            key in event.relation.data[event.app] for key in ("ue-imsi", "ue-status")
        ):
            ue_imsi = event.relation.data[event.app]["ue-imsi"]
            ue_status = event.relation.data[event.app]["ue-status"]
            if ue_status == "started":
                self._wait_ue_is_registered(ue_imsi)
                event.relation.data[self.app][ue_imsi] = "registered"

    def _update_service(self, event):
        try:
            logger.info("Updating service...")
            if not self.service_exists():
                logger.warning("service does not exist")
                return
            # Load data from dependent relations
            self._load_nrf_data()
            self._load_db_data()
            relations_ready = self.is_nrf_ready and self.is_db_ready
            if not relations_ready:
                self.unit.status = BlockedStatus("need nrf and db relations")
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
        if pod_ip := self.pod_ip:
            for relation in self.framework.model.relations["amf"]:
                logger.debug(f"Found relation {relation.name} with id {relation.id}")
                relation.data[self.app]["host"] = self.app.name
                relation.data[self.app]["ip-address"] = str(pod_ip)
                relation.data[self.app]["port"] = str(HTTP1_PORT)
                relation.data[self.app]["api-version"] = "v1"
                logger.info(
                    f"Info provided in relation {relation.name} (id {relation.id})"
                )

    def _clear_service_info(self):
        for relation in self.framework.model.relations["amf"]:
            logger.debug(f"Found relation {relation.name} with id {relation.id}")
            relation.data[self.app]["host"] = ""
            relation.data[self.app]["ip-address"] = ""
            relation.data[self.app]["port"] = ""
            relation.data[self.app]["api-version"] = ""
            logger.info(f"Info cleared in relation {relation.name} (id {relation.id})")

    def _wait_gnb_is_registered(self, gnb_name):
        self.unit.status = WaitingStatus(f"waiting for gnb {gnb_name} to be registered")
        self.search_logs(subsets_in_line={"Connected", gnb_name}, wait=True)
        self.unit.status = ActiveStatus()

    def _wait_ue_is_registered(self, ue_imsi):
        self.unit.status = WaitingStatus(f"waiting for ue {ue_imsi} to be registered")
        self.search_logs(subsets_in_line={"5GMM-REGISTERED", ue_imsi}, wait=True)
        self.unit.status = ActiveStatus()

    def _wait_until_service_is_active(self):
        logger.debug("Waiting for service to be active...")
        self.unit.status = WaitingStatus("Waiting for service to be active...")
        active = self.search_logs(
            {
                "amf_n2 started",
                "amf_n11 started",
                "Initiating all registered modules",
                "-----gNBs' information----",
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
    def is_db_ready(self):
        is_ready = (
            self._stored.db_host
            and self._stored.db_port
            and self._stored.db_user
            and self._stored.db_password
            and self._stored.db_database
        )
        logger.info(f'db is{" " if is_ready else " not "}ready')
        return is_ready

    def _load_db_data(self):
        logger.debug("Loading db data from relation")
        relation = self.framework.model.get_relation("db")
        if relation and relation.app in relation.data:
            relation_data = relation.data[relation.app]
            self._stored.db_host = relation_data.get("host")
            self._stored.db_port = relation_data.get("port")
            self._stored.db_user = relation_data.get("user")
            self._stored.db_password = relation_data.get("password")
            self._stored.db_database = relation_data.get("database")
            logger.info("db data loaded")
        else:
            self._stored.db_host = None
            self._stored.db_port = None
            self._stored.db_user = None
            self._stored.db_password = None
            self._stored.db_database = None
            logger.warning("no relation found")

    def _configure_service(self):
        if not self.service_exists():
            logger.debug("Cannot configure service: service does not exist yet")
            return
        logger.debug("Configuring amf service")
        container = self.unit.get_container("amf")
        container.add_layer(
            "oai_amf",
            {
                "services": {
                    "oai_amf": {
                        "override": "merge",
                        "environment": {
                            "NRF_FQDN": self._stored.nrf_host,
                            "NRF_IPV4_ADDRESS": "0.0.0.0",
                            "NRF_PORT": self._stored.nrf_port,
                            "NRF_API_VERSION": self._stored.nrf_api_version,
                            "MYSQL_SERVER": f"{self._stored.db_host}",
                            "MYSQL_USER": self._stored.db_user,
                            "MYSQL_PASS": self._stored.db_password,
                            "MYSQL_DB": self._stored.db_database,
                        },
                    }
                },
            },
            combine=True,
        )
        logger.info("amf service configured")

    def _add_oai_amf_layer(self, container):
        entrypoint = "/bin/bash /openair-amf/bin/entrypoint.sh"
        command = " ".join(
            ["/openair-amf/bin/oai_amf", "-c", "/openair-amf/etc/amf.conf", "-o"]
        )
        pebble_layer = {
            "summary": "oai_amf layer",
            "description": "pebble config layer for oai_amf",
            "services": {
                "oai_amf": {
                    "override": "replace",
                    "summary": "oai_amf",
                    "command": f"{entrypoint} {command}",
                    "environment": {
                        "DEBIAN_FRONTEND": "noninteractive",
                        "TZ": "Europe/Paris",
                        "INSTANCE": "0",
                        "PID_DIRECTORY": "/var/run",
                        "MCC": self.config["mcc"],
                        "MNC": self.config["mnc"],
                        "REGION_ID": self.config["region-id"],
                        "AMF_SET_ID": self.config["amf-set-id"],
                        "SERVED_GUAMI_MCC_0": self.config["served-guami-mcc-0"],
                        "SERVED_GUAMI_MNC_0": self.config["served-guami-mnc-0"],
                        "SERVED_GUAMI_REGION_ID_0": self.config["served-guami-region-id-0"],
                        "SERVED_GUAMI_AMF_SET_ID_0": self.config["served-guami-amf-set-id-0"],
                        "SERVED_GUAMI_MCC_1": self.config["served-guami-mcc-1"],
                        "SERVED_GUAMI_MNC_1": self.config["served-guami-mnc-1"],
                        "SERVED_GUAMI_REGION_ID_1": self.config["served-guami-region-id-1"],
                        "SERVED_GUAMI_AMF_SET_ID_1": self.config["served-guami-amf-set-id-1"],
                        "PLMN_SUPPORT_MCC": self.config["plmn-support-mcc"],
                        "PLMN_SUPPORT_MNC": self.config["plmn-support-mnc"],
                        "PLMN_SUPPORT_TAC": self.config["plmn-support-tac"],
                        "SST_0": self.config["sst-0"],
                        "SD_0": self.config["sd-0"],
                        "SST_1": self.config["sst-1"],
                        "SD_1": self.config["sd-1"],
                        "AMF_INTERFACE_NAME_FOR_NGAP": self.config["amf-interface-name-for-ngap"],
                        "AMF_INTERFACE_NAME_FOR_N11": self.config["amf-interface-name-for-n11"],
                        "SMF_INSTANCE_ID_0": self.config["smf-instance-id-0"],
                        "SMF_IPV4_ADDR_0": self.config["smf-ipv4-addr-0"],
                        "SMF_HTTP_VERSION_0": self.config["smf-http-version-0"],
                        "SMF_FQDN_0": self.config["smf-fqdn-0"],
                        "SMF_INSTANCE_ID_1": self.config["smf-instance-id-1"],
                        "SMF_IPV4_ADDR_1": self.config["smf-ipv4-addr-1"],
                        "SMF_HTTP_VERSION_1": self.config["smf-http-version-1"],
                        "SMF_FQDN_1": self.config["smf-fqdn-1"],
                        "AUSF_IPV4_ADDRESS": self.config["ausf-ipv4-address"],
                        "AUSF_PORT": self.config["ausf-port"],
                        "AUSF_API_VERSION": self.config["ausf-api-version"],
                        "AUSF_FQDN": self.config["ausf-fqdn"],  # TODO add support for ausf relation
                        "EXTERNAL_AUSF": self.config["external-ausf"],  # TODO add support for ausf relation
                        "NF_REGISTRATION": self.config["nf-registration"],
                        "SMF_SELECTION": self.config["smf-selection"],
                        "USE_FQDN_DNS": self.config["use-fqdn-dns"],
                        "OPERATOR_KEY": self.config["operator-key"],
                    },
                }
            },
        }
        container.add_layer("oai_amf", pebble_layer, combine=True)
        logger.info("oai_amf layer added")


if __name__ == "__main__":
    main(OaiAmfCharm, use_juju_for_storage=True)
