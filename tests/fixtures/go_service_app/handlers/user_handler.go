package handlers

import (
	"example.com/app/services"
)

// HandleCreateUser delegates to the service package.
func HandleCreateUser(name string) string {
	return services.CreateUser(name)
}
