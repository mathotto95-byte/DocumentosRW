const DATA_URL = "data/vencimentos_proximos.json";
const INTERVALO_ATUALIZACAO_MS = 5 * 60 * 1000;

const painel = document.getElementById("painel-vencimentos-proximos");
const botaoFullscreen = document.getElementById("botao-fullscreen");
const corpoTabela = document.getElementById("corpo-vencimentos");
const mensagemPainel = document.getElementById("mensagem-painel");
const ultimaAtualizacao = document.getElementById("ultima-atualizacao");

function escaparTexto(valor) {
  return String(valor ?? "").replace(/[&<>"']/g, (caractere) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[caractere]);
}

function formatarAtualizacao(valor) {
  if (!valor) return "Última atualização não informada";
  const data = new Date(valor);
  if (Number.isNaN(data.getTime())) return `Última atualização: ${valor}`;
  return `Última atualização: ${data.toLocaleString("pt-BR")}`;
}

function normalizarPayload(payload) {
  if (Array.isArray(payload)) {
    return { atualizado_em: null, registros: payload };
  }
  return {
    atualizado_em: payload?.atualizado_em ?? null,
    registros: Array.isArray(payload?.registros) ? payload.registros : [],
  };
}

function ordenarRegistros(registros) {
  return [...registros].sort((a, b) => (
    Number(a.dias_restantes ?? 9999) - Number(b.dias_restantes ?? 9999)
    || String(a.data_vencimento ?? "").localeCompare(String(b.data_vencimento ?? ""))
    || String(a.tipo_documento ?? "").localeCompare(String(b.tipo_documento ?? ""))
    || String(a.placas_composicao ?? "").localeCompare(String(b.placas_composicao ?? ""))
  ));
}

function renderizarRegistros(registros) {
  if (!registros.length) {
    corpoTabela.innerHTML = "";
    mensagemPainel.hidden = false;
    mensagemPainel.textContent = "Nenhum documento vencido ou com vencimento nos próximos 30 dias.";
    return;
  }

  mensagemPainel.hidden = true;
  corpoTabela.innerHTML = ordenarRegistros(registros).map((registro) => `
    <tr class="${Number(registro.dias_restantes) < 0 ? "linha-vencida" : ""}">
      <td>${escaparTexto(registro.tipo_documento)}</td>
      <td>${escaparTexto(registro.placas_composicao)}</td>
      <td>${escaparTexto(registro.data_vencimento)}</td>
      <td>${escaparTexto(registro.texto_dias_restantes)}</td>
    </tr>
  `).join("");
}

async function carregarDados() {
  try {
    const resposta = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!resposta.ok) {
      throw new Error(`HTTP ${resposta.status}`);
    }
    const payload = normalizarPayload(await resposta.json());
    renderizarRegistros(payload.registros);
    ultimaAtualizacao.textContent = formatarAtualizacao(payload.atualizado_em);
  } catch (erro) {
    corpoTabela.innerHTML = "";
    mensagemPainel.hidden = false;
    mensagemPainel.textContent = "Não foi possível carregar os dados de vencimentos.";
    ultimaAtualizacao.textContent = "Falha na atualização dos dados";
    console.error("Erro ao carregar vencimentos próximos", erro);
  }
}

function fullscreenAtivo() {
  return document.fullscreenElement === painel;
}

function atualizarBotaoFullscreen() {
  botaoFullscreen.textContent = fullscreenAtivo()
    ? "Sair da tela cheia"
    : "Tela cheia";
}

botaoFullscreen.addEventListener("click", async () => {
  try {
    if (fullscreenAtivo()) {
      await document.exitFullscreen();
    } else {
      await painel.requestFullscreen();
    }
  } catch (erro) {
    console.error("Não foi possível alternar tela cheia", erro);
  }
});

document.addEventListener("fullscreenchange", atualizarBotaoFullscreen);

carregarDados();
atualizarBotaoFullscreen();
window.setInterval(carregarDados, INTERVALO_ATUALIZACAO_MS);
