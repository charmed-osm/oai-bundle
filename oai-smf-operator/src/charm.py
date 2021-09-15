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

from kubernetes_service import K8sServicePatch, PatchFailed

logger = logging.getLogger(__name__)

SCTP_PORT = 38412
HTTP1_PORT = 80
HTTP2_PORT = 9090


class OaiSmfCharm(CharmBase):
    """Charm the service."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        event_observer_mapping = {
            self.on.smf_pebble_ready: self._on_oai_smf_pebble_ready,
            self.on.tcpdump_pebble_ready: self._on_tcpdump_pebble_ready,
            self.on.install: self._on_install,
            self.on.config_changed: self._on_config_changed,
            self.on.amf_relation_changed: self._update_service,
            self.on.amf_relation_broken: self._update_service,
            self.on.nrf_relation_changed: self._update_service,
            self.on.nrf_relation_broken: self._update_service,
            self.on.smf_relation_joined: self._provide_service_info,
        }
        for event, observer in event_observer_mapping.items():
            self.framework.observe(event, observer)
        self._stored.set_default(
            amf_host=None,
            amf_port=None,
            amf_api_version=None,
            nrf_host=None,
            nrf_port=None,
            nrf_api_version=None,
            _k8s_stateful_patched=False,
            _k8s_authed=False,
        )

    ####################################
    # Observers - Relation Events
    ####################################

    def _provide_service_info(self, event):
        if self.unit.is_leader() and self.is_service_running:
            for relation in self.framework.model.relations["smf"]:
                logger.info(f"Found relation {relation.name} with id {relation.id}")
                relation.data[self.app]["ready"] = str(True)
            else:
                logger.info("not relations found")

    ####################################
    # Observers - Charm Events
    ####################################

    def _on_install(self, event):
        self._k8s_auth()
        self._patch_stateful_set()
        K8sServicePatch.set_ports(
            self.app.name,
            [
                ("oai-smf", 8805, 8805, "UDP"),
                ("http1", 80, 80, "TCP"),
                ("http2", 9090, 9090, "TCP"),
            ],
        )

    def _on_config_changed(self, event):
        self._update_tcpdump_service(event)

    ####################################
    # Observers - Pebble Events
    ####################################

    def _on_oai_smf_pebble_ready(self, event):
        container = event.workload
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
                        "SMF_INTERFACE_NAME_FOR_N4": "eth0",
                        "SMF_INTERFACE_NAME_FOR_SBI": "eth0",
                        "SMF_INTERFACE_PORT_FOR_SBI": "80",
                        "SMF_INTERFACE_HTTP2_PORT_FOR_SBI": "9090",
                        "SMF_API_VERSION": "v1",
                        "DEFAULT_DNS_IPV4_ADDRESS": "8.8.8.8",
                        "DEFAULT_DNS_SEC_IPV4_ADDRESS": "8.8.4.4",
                        "REGISTER_NRF": "yes",
                        "DISCOVER_UPF": "yes",
                        "USE_FQDN_DNS": "yes",
                        "UDM_IPV4_ADDRESS": "127.0.0.1",
                        "UDM_PORT": "80",
                        "UDM_API_VERSION": "v1",
                        "UDM_FQDN": "localhost",
                        "UPF_IPV4_ADDRESS": "127.0.0.1",
                        "UPF_FQDN_0": "localhost",
                    },
                }
            },
        }
        try:
            container.add_layer("oai_smf", pebble_layer, combine=True)
            self._update_service(event)
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()
            return

    def _on_tcpdump_pebble_ready(self, event):
        self._update_tcpdump_service(event)

    ####################################
    # Properties
    ####################################

    @property
    def is_amf_ready(self):
        return (
            self._stored.amf_host
            and self._stored.amf_port
            and self._stored.amf_api_version
        )

    @property
    def is_nrf_ready(self):
        return (
            self._stored.nrf_host
            and self._stored.nrf_port
            and self._stored.nrf_api_version
        )

    @property
    def namespace(self) -> str:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace", "r") as f:
            return f.read().strip()

    @property
    def pod_ip(self) -> Optional[IPv4Address]:
        ip = check_output(["unit-get", "private-address"]).decode().strip()
        return IPv4Address(ip) if ip else None

    @property
    def container_name(self):
        return "smf"

    @property
    def service_name(self):
        return "oai_smf"

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
        self._load_amf_data()
        self._load_nrf_data()
        if self.is_nrf_ready and self.is_amf_ready:
            try:
                self._configure_service()
            except ConnectionError:
                logger.info("pebble socket not available, deferring config-changed")
                event.defer()
                return
            if self._start_service(container_name="smf", service_name="oai_smf"):
                self._provide_service_info(event)
                self.unit.status = ActiveStatus()
        else:
            self._stop_service(container_name="smf", service_name="oai_smf")
            self.unit.status = BlockedStatus("need nrf and amf relations")

    def _load_amf_data(self):
        relation = self.framework.model.get_relation("amf")
        if relation and relation.app in relation.data:
            relation_data = relation.data[relation.app]
            self._stored.amf_host = relation_data.get("host")
            self._stored.amf_port = relation_data.get("port")
            self._stored.amf_api_version = relation_data.get("api-version")
        else:
            self._stored.amf_host = None
            self._stored.amf_port = None
            self._stored.amf_api_version = None

    def _load_nrf_data(self):
        relation = self.framework.model.get_relation("nrf")
        if relation and relation.app in relation.data:
            relation_data = relation.data[relation.app]
            self._stored.nrf_host = relation_data.get("host")
            self._stored.nrf_port = relation_data.get("port")
            self._stored.nrf_api_version = relation_data.get("api-version")
        else:
            self._stored.nrf_host = None
            self._stored.nrf_port = None
            self._stored.nrf_api_version = None

    def _configure_service(self):
        container = self.unit.get_container("smf")
        if self.service_name in container.get_plan().services:
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

    def _start_service(self, container_name, service_name):
        container = self.unit.get_container(container_name)
        service_exists = service_name in container.get_plan().services
        is_running = container.get_service(service_name).is_running()

        if service_exists and not is_running:
            logger.info(f"{container.get_plan()}")
            container.start(service_name)
            time.sleep(30)
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
    # Utils - TCP Dump configuration
    ####################################

    def _update_tcpdump_service(self, event):
        try:
            self._configure_tcpdump_service()
        except ConnectionError:
            logger.info("pebble socket not available, deferring config-changed")
            event.defer()
            return
        if self.config["start-tcpdump"]:
            self._start_service("tcpdump", "tcpdump")
        else:
            self._stop_service("tcpdump", "tcpdump")

    def _configure_tcpdump_service(self):
        container = self.unit.get_container("tcpdump")
        container.add_layer(
            "tcpdump",
            {
                "summary": "tcpdump layer",
                "description": "pebble config layer for tcpdump",
                "services": {
                    "tcpdump": {
                        "override": "replace",
                        "summary": "tcpdump",
                        "command": f"/usr/sbin/tcpdump -i any -w /pcap_{self.app.name}.pcap",
                        "environment": {
                            "DEBIAN_FRONTEND": "noninteractive",
                            "TZ": "Europe/Paris",
                        },
                    }
                },
            },
            combine=True,
        )

    ####################################
    # Utils - K8s authentication
    ###########################

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
    main(OaiSmfCharm)
