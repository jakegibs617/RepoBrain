const { createUser, getUser } = require('./userService');

test('creates a user with a name', () => {
  const user = createUser({ name: 'ada' });
  expect(user.id).toBeGreaterThan(0);
  expect(user.name).toBe('ada');
});

test('returns null for a missing user', () => {
  expect(getUser(999999)).toBeNull();
});
