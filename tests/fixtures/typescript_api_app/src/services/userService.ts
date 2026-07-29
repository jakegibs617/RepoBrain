export function databaseUrl(): string {
  return process.env.DATABASE_URL ?? "sqlite://memory";
}

export function createUser(name: string): string {
  databaseUrl();
  return name;
}

// Never called: pins that an unreferenced export gets no inbound CALLS edge.
export function deleteUser(name: string): boolean {
  return name.length > 0;
}
