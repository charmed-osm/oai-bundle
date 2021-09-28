# OPENAIR-CN-5G: An implementation of the 5G Core network by the OpenAirInterface community.

This bundle deploys the OPENAIR-CN-5G charm operators.

## Deployment

```bash
juju add-model oai
juju deploy ch:oai --channel edge --trust
```

Verify the deployment has been successfully deployed:

```bash
$ juju status
Model    Controller  Cloud/Region        Version  SLA          Timestamp
oa       osm-vca     microk8s/localhost  2.9.9    unsupported  14:59:42+02:00

App         Version  Status  Scale  Charm           Store     Channel  Rev  OS          Address         Message
amf                  active      1  oai-amf         charmhub  edge       2  kubernetes  10.152.183.250
db                   active      1  oai-db          charmhub  edge       2  kubernetes  10.152.183.21
gnb                  active      1  oai-gnb         charmhub  edge       2  kubernetes  10.152.183.108
nr-ue                active      1  oai-nr-ue       charmhub  edge       2  kubernetes  10.152.183.16
nrf                  active      1  oai-nrf         charmhub  edge       2  kubernetes  10.152.183.105
smf                  active      1  oai-smf         charmhub  edge       2  kubernetes  10.152.183.73
spgwu-tiny           active      1  oai-spgwu-tiny  charmhub  edge       2  kubernetes  10.152.183.243

Unit           Workload  Agent  Address       Ports  Message
amf/0*         active    idle   10.1.245.70
db/0*          active    idle   10.1.245.109
gnb/0*         active    idle   10.1.245.85          registered
nr-ue/0*       active    idle   10.1.245.107         registered
nrf/0*         active    idle   10.1.245.80
smf/0*         active    idle   10.1.245.125
spgwu-tiny/0*  active    idle   10.1.245.118
```

## Test network connectivity from UE

```bash
$ kubectl -n oai exec -it nr-ue-0 -c nr-ue -- ping -I oaitun_ue1 google.fr -c 1
PING google.fr (142.250.201.67) from 12.1.1.129 oaitun_ue1: 56(84) bytes of data.
64 bytes from mad07s25-in-f3.1e100.net (142.250.201.67): icmp_seq=1 ttl=115 time=23.2 ms

--- google.fr ping statistics ---
1 packets transmitted, 1 received, 0% packet loss, time 0ms
rtt min/avg/max/mdev = 23.250/23.250/23.250/0.000 ms
```

## Scale the GNB

```bash
juju scale-application gnb 3
```

## Start and stop UE

Stop:

```bash
$ juju run-action nr-ue/0 stop --wait
unit-nr-ue-0:
  UnitId: nr-ue/0
  id: "2"
  results:
    output: service has been stopped successfully
  status: completed
  timing:
    completed: 2021-09-28 13:02:34 +0000 UTC
    enqueued: 2021-09-28 13:02:29 +0000 UTC
    started: 2021-09-28 13:02:32 +0000 UTC
$ kubectl -n oai exec -it nr-ue-0 -c nr-ue -- ping -I oaitun_ue1 google.fr -c 1
ping: SO_BINDTODEVICE: Invalid argument
command terminated with exit code 2
```

Start:

```bash
$ juju run-action nr-ue/0 start --wait
unit-nr-ue-0:
  UnitId: nr-ue/0
  id: "4"
  results:
    output: service has been started successfully
  status: completed
  timing:
    completed: 2021-09-28 13:03:38 +0000 UTC
    enqueued: 2021-09-28 13:03:32 +0000 UTC
    started: 2021-09-28 13:03:36 +0000 UTC
$ kubectl -n oai exec -it nr-ue-0 -c nr-ue -- ping -I oaitun_ue1 google.fr -c 1
PING google.fr (142.250.185.3) from 12.1.1.130 oaitun_ue1: 56(84) bytes of data.
64 bytes from mad41s11-in-f3.1e100.net (142.250.185.3): icmp_seq=1 ttl=115 time=25.2 ms

--- google.fr ping statistics ---
1 packets transmitted, 1 received, 0% packet loss, time 0ms
rtt min/avg/max/mdev = 25.205/25.205/25.205/0.000 ms
```

# Reference links

- https://gitlab.eurecom.fr/oai/cn5g/oai-cn5g-fed
