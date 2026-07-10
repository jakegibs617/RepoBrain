const express = require('express');
const config = require('./config');
const usersRouter = require('./routes/users');

const app = express();
app.use(express.json());
app.use(usersRouter);

function start() {
  app.listen(config.port, () => {
    console.log(`listening on ${config.port}`);
  });
}

if (require.main === module) {
  start();
}

module.exports = { app, start };
