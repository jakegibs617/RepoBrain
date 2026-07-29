package com.example.services;

public class UserService {

    public static String createUser(String name) {
        return name;
    }

    // Never referenced anywhere: pins that an uncalled method gets no
    // inbound CALLS edge.
    public static boolean deleteUser(String name) {
        return name != null;
    }

    public String describe() {
        return "user service";
    }
}
