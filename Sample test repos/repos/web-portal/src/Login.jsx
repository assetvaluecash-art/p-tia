import { useState } from "react";
import { useNavigate } from "react-router-dom";

export default function Login() {
  const navigate = useNavigate();
  const [user, setUser] = useState("");
  const signIn = () => navigate("/accounts");
  return (
    <form onSubmit={e => { e.preventDefault(); signIn(); }}>
      <label htmlFor="username">User name</label>
      <input id="username" name="username" required minLength={3} maxLength={30} onChange={e => setUser(e.target.value)} />
      <label htmlFor="password">Password</label>
      <input id="password" name="password" type="password" required minLength={8} />
      <button type="submit" data-testid="sign-in">Sign in</button>
    </form>
  );
}
