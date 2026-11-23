import { Button, Card, Field, Input, Spinner, Text, Title2, makeStyles, shorthands, tokens } from "@fluentui/react-components";
import { MailRegular, ShieldKeyholeRegular } from "@fluentui/react-icons";
import { FormEvent, useState } from "react";
import { useOTPAuth } from "../context/OTPAuthContext";

const useStyles = makeStyles({
  page: {
    minHeight: "100vh",
    display: "grid",
    placeItems: "center",
    background: tokens.colorNeutralBackground2,
    padding: "24px",
  },
  card: {
    width: "min(420px, 100%)",
    ...shorthands.borderRadius("8px"),
    ...shorthands.padding("28px"),
  },
  brand: {
    display: "flex",
    alignItems: "center",
    gap: "12px",
    marginBottom: "20px",
  },
  logo: {
    height: "54px",
    width: "auto",
  },
  stack: {
    display: "grid",
    gap: "16px",
  },
  message: {
    color: tokens.colorNeutralForeground3,
  },
  error: {
    color: tokens.colorPaletteRedForeground1,
  },
});

export default function LoginPage() {
  const cls = useStyles();
  const { requestCode, verifyCode } = useOTPAuth();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  async function submitEmail(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await requestCode(email);
      setSent(true);
      setMessage("Verification code sent.");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function submitCode(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await verifyCode(email, code);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={cls.page}>
      <Card className={cls.card}>
        <div className={cls.brand}>
          <img src="/momentlens-logo.svg" alt="MomentLens" className={cls.logo} />
          <Title2>MomentLens</Title2>
        </div>
        <form className={cls.stack} onSubmit={sent ? submitCode : submitEmail}>
          <Field label="Work email" required>
            <Input
              type="email"
              value={email}
              onChange={(_, data) => setEmail(data.value)}
              disabled={busy || sent}
              contentBefore={<MailRegular />}
              autoComplete="email"
            />
          </Field>
          {sent && (
            <Field label="Verification code" required>
              <Input
                value={code}
                onChange={(_, data) => setCode(data.value.replace(/\D/g, "").slice(0, 6))}
                disabled={busy}
                contentBefore={<ShieldKeyholeRegular />}
                inputMode="numeric"
                autoComplete="one-time-code"
              />
            </Field>
          )}
          {message && <Text className={cls.message}>{message}</Text>}
          {error && <Text className={cls.error}>{error}</Text>}
          <Button appearance="primary" type="submit" disabled={busy || !email || (sent && code.length !== 6)}>
            {busy ? <Spinner size="tiny" /> : sent ? "Verify" : "Send code"}
          </Button>
          {sent && (
            <Button appearance="subtle" type="button" disabled={busy} onClick={() => { setSent(false); setCode(""); setMessage(""); }}>
              Use another email
            </Button>
          )}
        </form>
      </Card>
    </div>
  );
}
