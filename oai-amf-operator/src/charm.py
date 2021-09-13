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
from subprocess import check_output
from typing import Optional
import time

from kubernetes import kubernetes
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus
from ops.pebble import ConnectionError


logger = logging.getLogger(__name__)

SCTP_PORT = 38412
HTTP1_PORT = 80
HTTP2_PORT = 9090


class OaiAmfCharm(CharmBase):
    """Charm the service."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        event_observer_mapping = {
            self.on.amf_pebble_ready: self._on_oai_amf_pebble_ready,
            self.on.tcpdump_pebble_ready: self._on_tcpdump_pebble_ready,
            self.on.install: self._on_install,
            self.on.config_changed: self._on_config_changed,
            self.on.amf_relation_joined: self._provide_service_info,
            self.on.nrf_relation_changed: self._update_service,
            self.on.nrf_relation_broken: self._update_service,
            self.on.db_relation_changed: self._update_service,
            self.on.db_relation_broken: self._update_service,
        }
        for event, observer in event_observer_mapping.items():
            self.framework.observe(event, observer)
        self._stored.set_default(
            nrf_host=None,
            nrf_port=None,
            db_host=None,
            db_port=None,
            db_user=None,
            db_password=None,
            db_database=None,
            nrf_api_version=None,
            _k8s_stateful_patched=False,
            _k8s_authed=False,
        )

    ####################################
    # Observers - Relation Events
    ####################################

    def _provide_service_info(self, event):
        if not self.unit.is_leader():
            return
        pod_ip = self.pod_ip
        if pod_ip:
            event.relation.data[self.app]["host"] = str(pod_ip)
            event.relation.data[self.app]["port"] = str(HTTP1_PORT)
            event.relation.data[self.app]["api-version"] = "v1"
        else:
            event.defer()

    def _update_service(self, event):
        self._load_nrf_data()
        self._load_db_data()
        if self.is_nrf_ready and self.is_db_ready:
            try:
                self._configure_service()
            except ConnectionError:
                logger.info("pebble socket not available, deferring config-changed")
                event.defer()
                return
            self._start_service(container_name="amf", service_name="oai_amf")
            self.unit.status = ActiveStatus()
        else:
            self._stop_service(container_name="amf", service_name="oai_amf")
            self.unit.status = BlockedStatus("need nrf and db relations")

    ####################################
    # Observers - Charm Events
    ####################################

    def _on_install(self, event):
        self._k8s_auth()
        self._patch_stateful_set()

    def _on_config_changed(self, _):
        if self.config["start-tcpdump"]:
            self._start_service("tcpdump", "tcpdump")
        else:
            self._stop_service("tcpdump", "tcpdump")

    ####################################
    # Observers - Pebble Events
    ####################################

    def _on_oai_amf_pebble_ready(self, event):
        container = event.workload
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
                        "MCC": "208",
                        "MNC": "95",
                        "REGION_ID": "128",
                        "AMF_SET_ID": "1",
                        "SERVED_GUAMI_MCC_0": "208",
                        "SERVED_GUAMI_MNC_0": "95",
                        "SERVED_GUAMI_REGION_ID_0": "128",
                        "SERVED_GUAMI_AMF_SET_ID_0": "1",
                        "SERVED_GUAMI_MCC_1": "460",
                        "SERVED_GUAMI_MNC_1": "11",
                        "SERVED_GUAMI_REGION_ID_1": "10",
                        "SERVED_GUAMI_AMF_SET_ID_1": "1",
                        "PLMN_SUPPORT_MCC": "208",
                        "PLMN_SUPPORT_MNC": "95",
                        "PLMN_SUPPORT_TAC": "0xa000",
                        "SST_0": "222",
                        "SD_0": "123",
                        "SST_1": "111",
                        "SD_1": "124",
                        "AMF_INTERFACE_NAME_FOR_NGAP": "eth0",
                        "AMF_INTERFACE_NAME_FOR_N11": "eth0",
                        "SMF_INSTANCE_ID_0": "1",
                        "SMF_IPV4_ADDR_0": "127.0.0.1",
                        "SMF_HTTP_VERSION_0": "v1",
                        "SMF_FQDN_0": "localhost",
                        "SMF_INSTANCE_ID_1": "2",
                        "SMF_IPV4_ADDR_1": "127.0.0.1",
                        "SMF_HTTP_VERSION_1": "v1",
                        "SMF_FQDN_1": "localhost",
                        "NRF_FQDN": "oai-nrf-svc",
                        "AUSF_IPV4_ADDRESS": "127.0.0.1",
                        "AUSF_PORT": 80,
                        "AUSF_API_VERSION": "v1",
                        "NF_REGISTRATION": "yes",
                        "SMF_SELECTION": "yes",
                        "USE_FQDN_DNS": "no",
                        "OPERATOR_KEY": "63bfa50ee6523365ff14c1f45f88737d",
                    },
                }
            },
        }
        try:
            container.add_layer("oai_amf", pebble_layer, combine=True)
            self._update_service(event)
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()
            return

    def _on_tcpdump_pebble_ready(self, event):
        container = event.workload
        command = f"/usr/sbin/tcpdump -i any -w /pcap_{self.app.name}.pcap"
        pebble_layer = {
            "summary": "tcpdump layer",
            "description": "pebble config layer for tcpdump",
            "services": {
                "tcpdump": {
                    "override": "replace",
                    "summary": "tcpdump",
                    "command": command,
                    "environment": {
                        "DEBIAN_FRONTEND": "noninteractive",
                        "TZ": "Europe/Paris",
                    },
                }
            },
        }
        try:
            container.add_layer("tcpdump", pebble_layer, combine=True)
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()
            return

    ####################################
    # Properties
    ####################################

    @property
    def is_nrf_ready(self):
        return (
            self._stored.nrf_host
            and self._stored.nrf_port
            and self._stored.nrf_api_version
        )

    @property
    def is_db_ready(self):
        return (
            self._stored.db_host
            and self._stored.db_port
            and self._stored.db_user
            and self._stored.db_password
            and self._stored.db_database
        )

    @property
    def namespace(self) -> str:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace", "r") as f:
            return f.read().strip()

    @property
    def pod_ip(self) -> Optional[IPv4Address]:
        return IPv4Address(
            check_output(["unit-get", "private-address"]).decode().strip()
        )

    ####################################
    # Utils - Services and configuration
    ####################################

    def _load_nrf_data(self):
        relation = self.framework.model.get_relation("nrf")
        if relation:
            relation_data = relation.data[relation.app]
            self._stored.nrf_host = relation_data.get("host")
            self._stored.nrf_port = relation_data.get("port")
            self._stored.nrf_api_version = relation_data.get("api-version")
        else:
            self._stored.nrf_host = None
            self._stored.nrf_port = None
            self._stored.nrf_api_version = None

    def _load_db_data(self):
        relation = self.framework.model.get_relation("db")
        if relation:
            relation_data = relation.data[relation.app]
            self._stored.db_host = relation_data.get("host")
            self._stored.db_port = relation_data.get("port")
            self._stored.db_user = relation_data.get("user")
            self._stored.db_password = relation_data.get("password")
            self._stored.db_database = relation_data.get("database")
        else:
            self._stored.db_host = None
            self._stored.db_port = None
            self._stored.db_user = None
            self._stored.db_password = None
            self._stored.db_database = None

    def _configure_service(self):
        container = self.unit.get_container("amf")
        container.add_layer(
            "oai_amf",
            {
                "services": {
                    "oai_amf": {
                        "override": "merge",
                        "environment": {
                            "NRF_IPV4_ADDRESS": self._stored.nrf_host,
                            "NRF_PORT": self._stored.nrf_port,
                            "NRF_API_VERSION": self._stored.nrf_api_version,
                            "MYSQL_SERVER": f"{self._stored.db_host}:{self._stored.db_port}",
                            "MYSQL_USER": self._stored.db_user,
                            "MYSQL_PASS": self._stored.db_password,
                            "MYSQL_DB": self._stored.db_database,
                        },
                    }
                },
            },
            combine=True,
        )

    def _start_service(self, container_name, service_name):
        container = self.unit.get_container(container_name)
        is_running = (
            service_name in container.get_plan().services
            and container.get_service(service_name).is_running()
        )
        if not is_running:
            container.start(service_name)

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
    ####################################

    def _k8s_auth(self) -> bool:
        """Authenticate to kubernetes."""
        if self._stored._k8s_authed:
            return True
        kubernetes.config.load_incluster_config()
        self._stored._k8s_authed = True

    def _patch_stateful_set(self) -> None:
        """Patch the StatefulSet to include specific ServiceAccount and Secret mounts"""
        if self._stored._k8s_stateful_patched:
            return

        # Get an API client
        api = kubernetes.client.AppsV1Api(kubernetes.client.ApiClient())
        for attempt in range(5):
            try:
                self.unit.status = MaintenanceStatus(
                    f"patching StatefulSet for additional k8s permissions. Attempt {attempt+1}/5"
                )
                s = api.read_namespaced_stateful_set(
                    name=self.app.name, namespace=self.namespace
                )
                # Add the required security context to the container spec
                s.spec.template.spec.containers[1].security_context.privileged = True

                # Patch the StatefulSet with our modified object
                api.patch_namespaced_stateful_set(
                    name=self.app.name, namespace=self.namespace, body=s
                )
                logger.info(
                    "Patched StatefulSet to include additional volumes and mounts"
                )
                self._stored._k8s_stateful_patched = True
                return
            except Exception as e:
                self.unit.status = MaintenanceStatus(
                    "failed patching StatefulSet... Retrying in 10 seconds"
                )
                time.sleep(5)


if __name__ == "__main__":
    main(OaiAmfCharm, use_juju_for_storage=True)
