import { useEffect, useState } from "react";
import axios from "axios";

export default function Accounts() {
  const [account, setAccount] = useState("ACC1001");
  const [balance, setBalance] = useState(null);
  const [payments, setPayments] = useState([]);
  useEffect(() => {
    axios.get(`/accounts/${account}/balance`).then(r => setBalance(r.data));
    axios.get(`/accounts/${account}/payments`).then(r => setPayments(r.data));
  }, [account]);
  return (
    <main>
      <label htmlFor="account">Account</label>
      <select id="account" name="account" value={account} onChange={e => setAccount(e.target.value)}>
        <option value="ACC1001">ACC1001</option>
        <option value="ACC2002">ACC2002</option>
      </select>
      <a href="/pay" data-testid="make-payment">Make a payment</a>
    </main>
  );
}
