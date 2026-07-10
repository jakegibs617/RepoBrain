const config = require('../config');

const users = new Map();
let nextId = 1;

function createUser(payload) {
  if (!payload || !payload.name) {
    throw new Error('name is required');
  }
  const user = { id: nextId, name: payload.name };
  nextId += 1;
  users.set(user.id, user);
  return user;
}

function getUser(id) {
  return users.get(id) || null;
}

module.exports = { createUser, getUser };
