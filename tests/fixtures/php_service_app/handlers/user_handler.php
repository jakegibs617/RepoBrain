<?php

require_once __DIR__ . '/../services/user_service.php';

function handle_create_user($name) {
    return create_user($name);
}
