require_relative '../services/user_service'

def build_service
  UserService.new
end

def handle_create_user(name)
  # Parenthesized deliberately: tree-sitter parses a parenless `build_service`
  # as an identifier, not a call, so the extractor does not see it.
  service = build_service()
  # Variable receiver: deliberately unresolved.
  service.create_user(name)
end
