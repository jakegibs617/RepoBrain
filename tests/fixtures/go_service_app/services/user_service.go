package services

// Store persists users.
type Store struct {
	rows map[string]string
}

// Save writes one user and is only ever reached through a variable receiver,
// which the extractor deliberately does not resolve.
func (s *Store) Save(name string) string {
	s.rows[name] = name
	return name
}

func normalize(name string) string {
	return name
}

// CreateUser is the service entry point the handler calls.
func CreateUser(name string) string {
	store := &Store{rows: map[string]string{}}
	return store.Save(normalize(name))
}

// DeleteUser is never called: it pins that unreferenced functions produce no
// inbound CALLS edge.
func DeleteUser(name string) bool {
	return name != ""
}
