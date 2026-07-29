package com.example.handlers;

import com.example.services.UserService;

public class UserHandler {

    public String handleCreateUser(String name) {
        // Class-qualified static call: resolves through the name registry.
        return UserService.createUser(name);
    }

    public String handleDescribe() {
        UserService service = new UserService();
        // Variable-qualified call: tree-sitter cannot tell this from a
        // class-qualified one, so it must stay unresolved (D34).
        return service.describe();
    }
}
