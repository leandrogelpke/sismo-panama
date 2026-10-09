# sismo-panama — relatório sísmico automático de hora em hora

Roda no **GitHub Actions**, sem servidor e sem depender do seu computador. A cada hora:

1. Lê o catálogo do **USGS** (tremores na caixa Panamá, última hora, hora anterior e acumulado desde o sismo principal).
2. Lê a **previsão oficial de réplicas do USGS** (Operational Aftershock Forecast) — se ainda não existir, usa um modelo genérico e avisa que é estimativa.
3. Lê os boletins de **tsunami** do PTWC/NTWC (tsunami.gov).
4. Lê manchetes recentes do **Google News** ("sismo Panamá", "Sinaproc") — só títulos, sem abrir os sites.
5. Deriva **ações para as próximas horas** com regras (tsunami ativo, M6+ nas últimas 3 h, noite, 48 h iniciais…).
6. Envia: **e-mail** (Gmail), **WhatsApp** pessoal (CallMeBot) e, se quiser, **Telegram**.

Tudo em `report.py` (Python padrão, sem dependências). O workflow está em `.github/workflows/sismo.yml`.

## Configuração (uma vez)

Em **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Valor |
|---|---|
| `GMAIL_USER` | seu Gmail (remetente) |
| `GMAIL_APP_PASSWORD` | senha de app do Google (16 caracteres; crie em myaccount.google.com/apppasswords — exige verificação em 2 etapas) |
| `MAIL_TO` | destinatários separados por vírgula (pode incluir a esposa) |
| `CALLMEBOT_RECIPIENTS` | `+507xxxxxxxx:APIKEY;+507yyyyyyyy:APIKEY` — um par por pessoa |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | opcional |

**CallMeBot (WhatsApp pessoal, gratuito):** cada número salva o contato **+34 644 20 47 56** e envia a ele, pelo WhatsApp,
`I allow callmebot to send me messages`. A resposta traz a APIKEY daquele número. Não envia para grupos — cada pessoa recebe
no próprio WhatsApp. Serviço de terceiros, "melhor esforço": pode atrasar alguns minutos.

Variáveis opcionais (**Settings → Secrets and variables → Actions → Variables**):

- `MAINSHOCK_ID` — força o id USGS do sismo principal (por padrão, o maior M6,5+ dos últimos 30 dias na região).
- `QUIET_AFTER_HOURS` — horas após o principal para entrar em "hora tranquila" (padrão 72). Nesse modo o e-mail continua
  de hora em hora e o WhatsApp sai só às 00/06/12/18 h.

## Rodar agora / testar

**Actions → "Relatório sísmico Panamá" → Run workflow**. Com `dry_run = 1` ele só imprime o relatório no log (nada é enviado).
O relatório completo de cada execução fica no resumo do job.

## Parar ou mudar a cadência

- Parar: **Actions → Relatório sísmico Panamá → ⋯ → Disable workflow**.
- Reduzir para a cada 6 h: em `sismo.yml`, troque `"5 * * * *"` por `"5 */6 * * *"`.

## Limites que você precisa saber

- O GitHub atrasa jobs agendados em geral 3–15 min (às vezes mais em horários de pico). O horário no relatório é o real.
- Se não houver nenhum sismo M6,5+ nos últimos 30 dias na região, o job termina sem enviar nada.
- Nenhuma fonte é "verificada" pelo script: USGS e tsunami.gov são oficiais; as manchetes são só manchetes.
- Custo: zero (minutos gratuitos do GitHub Actions; ~1 min por execução).
