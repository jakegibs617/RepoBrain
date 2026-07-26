<?php

function database_url() {
    return getenv('DATABASE_URL');
}

function create_user($name) {
    database_url();
    return $name;
}

// Never called: pins that an unreferenced function gets no inbound CALLS edge.
function delete_user($name) {
    return $name !== null;
}
