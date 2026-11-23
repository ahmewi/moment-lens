import React from "react";
import ReactDOM from "react-dom/client";
import { FluentProvider } from "@fluentui/react-components";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { OTPAuthProvider } from "./context/OTPAuthContext";
import { momentLensLightTheme } from "./theme";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <FluentProvider theme={momentLensLightTheme}>
      <BrowserRouter>
        <OTPAuthProvider>
          <App />
        </OTPAuthProvider>
      </BrowserRouter>
    </FluentProvider>
  </React.StrictMode>
);
