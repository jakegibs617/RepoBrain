import { createUser } from "../services/userService";

export function handleCreateUser(name: string): string {
  return createUser(name);
}
