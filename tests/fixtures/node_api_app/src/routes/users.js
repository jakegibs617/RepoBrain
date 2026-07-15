const express = require('express');
const { createUser, getUser } = require('../services/userService');

const router = express.Router();

router.post('/api/users', (req, res) => {
  const user = createUser(req.body);
  res.status(201).json(user);
});

function getUserRoute(req, res) {
  const user = getUser(Number(req.params.id));
  if (!user) {
    return res.status(404).json({ error: 'not found' });
  }
  res.json(user);
}

router.get('/api/users/:id', getUserRoute);

module.exports = router;
