const config = {
  port: process.env.PORT || 3000,
  databaseUrl: process.env.DATABASE_URL,
  logLevel: process.env.LOG_LEVEL || 'info',
};

module.exports = config;
