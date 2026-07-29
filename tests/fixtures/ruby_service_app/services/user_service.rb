class UserService
  def create_user(name)
    name
  end

  # Never called: pins that an unreferenced method gets no inbound CALLS edge.
  def delete_user(name)
    !name.nil?
  end
end
