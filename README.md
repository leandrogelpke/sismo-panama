# sismo-panama — informe sísmico automático (texto em espanhol)

**Cadência:** o workflow roda a cada hora, mas só **envia** o informe regular a cada 3 h (00, 03, 06, 09, 12, 15, 18, 21 — hora do Panamá) e, fora desses horários, **alerta imediata** quando houver réplica ≥ M5.5 na última hora, boletim de tsunami ativo ou sequência reativada. As rodadas que não enviam continuam gravando o histórico. Variáveis: `SEND_EVERY_HOURS` (padrão 3), `ALERT_MIN_MAG` (padrão 5.5), `FORCE_SEND=1` para forçar um envio manual.

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

| Secret | Valor | Obrigatório? |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | token do bot criado no @BotFather (`/newbot`) | **sim** — é o canal de celular que não depende do Mac |
| `GMAIL_USER` | seu Gmail (remetente) | opcional |
| `GMAIL_APP_PASSWORD` | senha de app do Google (16 caracteres; myaccount.google.com/apppasswords — exige verificação em 2 etapas) | opcional |
| `MAIL_TO` | destinatários separados por vírgula | opcional |
| `CALLMEBOT_RECIPIENTS` | `+507xxxxxxxx:APIKEY;+507yyyyyyyy:APIKEY` | opcional — em out/2026 o número de ativação do CallMeBot passou a redirecionar para o serviço pago de chamadas; o canal de texto gratuito não é confiável |

**Telegram (recomendado):** crie o bot no @BotFather, cole o token no secret e depois **mande `/start` ao bot** (e/ou adicione o bot
a um grupo com a família e mande qualquer mensagem lá). Na rodada seguinte o script descobre sozinho os chats e grava em
`reports/telegram_chats.json`; a partir daí envia para todos eles. Para fixar um chat específico, use a variável `TELEGRAM_CHAT_ID`.

Variáveis opcionais (**Settings → Secrets and variables → Actions → Variables**):

- `MAINSHOCK_ID` — força o id USGS do sismo principal (por padrão, o maior M6,5+ dos últimos 30 dias na região).
- `QUIET_AFTER_HOURS` — horas após o principal para entrar em "hora tranquila" (padrão 72). Nesse modo o e-mail continua
  de hora em hora e o Telegram/WhatsApp saem só às 00/06/12/18 h.
- `TELEGRAM_CHAT_ID` — um ou mais chat ids separados por vírgula (desliga a descoberta automática).

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
