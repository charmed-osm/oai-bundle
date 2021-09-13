#!/bin/bash

function build() {
    charm=$1
    cd oai-$charm-operator/
    charmcraft build
    mv oai-${charm}_ubuntu-20.04-amd64.charm $charm.charm
    cd ..    
}

charms="nrf amf smf spgwu-tiny db"
for charm in $charms; do
    build $charm &
done

wait
