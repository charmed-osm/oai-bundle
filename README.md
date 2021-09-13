# OAI Operators

## Prepare environment

```bash
juju add-model oai-01
```

## Deployment

Build charms:

```bash
./build.sh
```

Deploy bundle:

```bash
juju deploy ./bundle.yaml --trust
```
