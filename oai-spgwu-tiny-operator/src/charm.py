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
import time
from typing import Optional

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


class OaiSpgwuTinyCharm(CharmBase):
    """Charm the service."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        event_observer_mapping = {
            self.on.spgwu_tiny_pebble_ready: self._on_oai_spgwu_tiny_pebble_ready,
            self.on.tcpdump_pebble_ready: self._on_tcpdump_pebble_ready,
            self.on.install: self._on_install,
            self.on.config_changed: self._on_config_changed,
            self.on.spgwu_relation_joined: self._provide_service_info,
            self.on.nrf_relation_changed: self._update_service,
            self.on.nrf_relation_broken: self._update_service,
            self.on.smf_relation_changed: self._update_service,
            self.on.smf_relation_broken: self._update_service,
        }
        for event, observer in event_observer_mapping.items():
            self.framework.observe(event, observer)
        self._stored.set_default(
            nrf_host=None,
            nrf_port=None,
            nrf_api_version=None,
            smf_ready=False,
            _k8s_stateful_patched=False,
            _k8s_authed=False,
        )

    ####################################
    # Observers - Relation Events
    ####################################

    def _provide_service_info(self, event):
        if self.unit.is_leader() and self.is_service_running:
            for relation in self.framework.model.relations["spgwu"]:
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
                ("oai-spgwu-tiny", 8805, 8805, "UDP"),
                ("s1u", 2152, 2152, "UDP"),
                ("iperf", 5001, 5001, "UDP"),
            ],
        )

    def _on_config_changed(self, event):
        self._update_tcpdump_service(event)

    ####################################
    # Observers - Pebble Events
    ####################################

    def _on_oai_spgwu_tiny_pebble_ready(self, event):
        container = event.workload
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
                        "GW_ID": "1",
                        "MCC": "208",
                        "MNC03": "95",
                        "REALM": "3gpp.org",
                        "PID_DIRECTORY": "/var/run",
                        "SGW_INTERFACE_NAME_FOR_S1U_S12_S4_UP": "eth0",
                        "THREAD_S1U_PRIO": "98",
                        "S1U_THREADS": "1",
                        "SGW_INTERFACE_NAME_FOR_SX": "eth0",
                        "THREAD_SX_PRIO": "98",
                        "SX_THREADS": "1",
                        "PGW_INTERFACE_NAME_FOR_SGI": "eth0",
                        "THREAD_SGI_PRIO": "98",
                        "SGI_THREADS": "1",
                        "NETWORK_UE_NAT_OPTION": "yes",
                        "GTP_EXTENSION_HEADER_PRESENT": "yes",
                        "NETWORK_UE_IP": "12.1.1.0/24",
                        "SPGWC0_IP_ADDRESS": "127.0.0.1",
                        "BYPASS_UL_PFCP_RULES": "no",
                        "ENABLE_5G_FEATURES": "yes",
                        "NSSAI_SST_0": "1",
                        "NSSAI_SD_0": "1",
                        "DNN_0": "oai",
                        "UPF_FQDN_5G": self.app.name,
                    },
                }
            },
        }
        try:
            container.add_layer("oai_spgwu_tiny", pebble_layer, combine=True)
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
    def is_nrf_ready(self):
        return (
            self._stored.nrf_host
            and self._stored.nrf_port
            and self._stored.nrf_api_version
        )

    @property
    def is_smf_ready(self):
        return self._stored.smf_ready

    @property
    def namespace(self) -> str:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace", "r") as f:
            return f.read().strip()

    @property
    def pod_ip(self) -> Optional[IPv4Address]:
        return IPv4Address(
            check_output(["unit-get", "private-address"]).decode().strip()
        )

    @property
    def container_name(self):
        return "spgwu-tiny"

    @property
    def service_name(self):
        return "oai_spgwu_tiny"

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
        self._load_nrf_data()
        self._load_smf_data()
        if self.is_nrf_ready and self.is_smf_ready:
            try:
                self._configure_service()
            except ConnectionError:
                logger.info("pebble socket not available, deferring config-changed")
                event.defer()
                return
            if self._start_service(
                container_name="spgwu-tiny", service_name="oai_spgwu_tiny"
            ):
                self._provide_service_info(event)
                self.unit.status = ActiveStatus()
        else:
            self._stop_service(
                container_name="spgwu-tiny", service_name="oai_spgwu_tiny"
            )
            self.unit.status = BlockedStatus("need nrf and smf relations")

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

    def _load_smf_data(self):
        relation = self.framework.model.get_relation("smf")
        if relation and relation.app in relation.data:
            relation_data = relation.data[relation.app]
            self._stored.smf_ready = relation_data.get("ready") == "True"
        else:
            self._stored.smf_ready = False

    def _configure_service(self):
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

    def _start_service(self, container_name, service_name):
        container = self.unit.get_container(container_name)
        service_exists = service_name in container.get_plan().services
        is_running = container.get_service(service_name).is_running()

        if service_exists and not is_running:
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
    main(OaiSpgwuTinyCharm, use_juju_for_storage=True)
