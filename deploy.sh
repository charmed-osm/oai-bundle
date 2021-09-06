#!/bin/bash

charms="nrf amf smf spgwu-tiny db"
for charm in $charms; do
    cd oai-$charm-operator/
    charmcraft build &
    cd ..    
done
wait

juju deploy ./bundle.yaml --trust