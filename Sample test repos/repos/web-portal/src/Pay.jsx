import { useState } from "react";
import { useNavigate } from "react-router-dom";
import axios from "axios";

export default function Pay() {
  const navigate = useNavigate();
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("EUR");
  const [reference, setReference] = useState("");
  const pay = () => axios.post("/payments", { account_id: "ACC1001", amount, currency, reference }).then(() => navigate("/done"));
  return (
    <form onSubmit={e => { e.preventDefault(); pay(); }}>
      <label htmlFor="amount">Amount</label>
      <input id="amount" name="amount" type="number" required min="0.01" max="10000" onChange={e => setAmount(e.target.value)} />
      <label htmlFor="currency">Currency</label>
      <select id="currency" name="currency" required value={currency} onChange={e => setCurrency(e.target.value)}>
        <option value="EUR">EUR</option>
        <option value="USD">USD</option>
        <option value="GBP">GBP</option>
      </select>
      <label htmlFor="reference">Reference</label>
      <input id="reference" name="reference" required maxLength={18} onChange={e => setReference(e.target.value)} />
      <button type="submit" data-testid="pay">Pay</button>
    </form>
  );
}
