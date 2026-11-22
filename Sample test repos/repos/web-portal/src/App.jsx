import { BrowserRouter, Routes, Route } from "react-router-dom";
import Login from "./Login";
import Accounts from "./Accounts";
import Pay from "./Pay";
import Done from "./Done";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/accounts" element={<Accounts />} />
        <Route path="/pay" element={<Pay />} />
        <Route path="/done" element={<Done />} />
      </Routes>
    </BrowserRouter>
  );
}
