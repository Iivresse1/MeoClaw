import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import UIPrototype from "./dev/UIPrototype";
import "./styles.css";

const prototypeVariant = import.meta.env.DEV
  ? new URLSearchParams(window.location.search).get("variant")
  : null;

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {prototypeVariant ? <UIPrototype /> : <App />}
  </React.StrictMode>
);
