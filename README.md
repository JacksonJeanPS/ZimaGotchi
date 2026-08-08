# ZimaGotchi — Demonstração (dados fictícios)

Esta pasta contém uma **demonstração estática** do painel do ZimaGotchi, feita
para ser publicada no GitHub Pages e mostrar como a interface se comporta —
sem precisar de um servidor ZimaOS, adaptador Wi‑Fi ou qualquer hardware real.

👉 **[Ver a demonstração ao vivo](https://jacksonjeanps.github.io/ZimaGotchi/)**

📦 **[Baixar o projeto em ZIP](https://github.com/JacksonJeanPS/ZimaGotchi/archive/refs/heads/main.zip)**

## O que esta demo é (e o que não é)

- É um único arquivo `index.html`, sem backend, sem build, sem dependências.
- Todo o "comportamento" do mascote — pacotes, XP, nível, troca de canal,
  handshakes, redes online/offline — é **gerado no seu navegador** com
  `Math.random()`, só para ilustrar a interface do projeto real.
- As seis redes mostradas (`CAFE-NET_Demo`, `SUNVILLA_Fibra` etc.) são
  fictícias. Os BSSIDs usam o prefixo `02:xx:xx:xx:xx:xx`, que pelo padrão
  IEEE 802 é um endereço "administrado localmente" — nunca corresponde a um
  fabricante real, ou seja, são exemplos que não apontam pra nenhum roteador
  de verdade.
- **Não captura Wi‑Fi, não acessa rede nenhuma, não envia dados a lugar
  algum.** É só JavaScript client-side rodando dados fictícios.
- Na demo, os canais alternam a cada ~9 segundos só para você ver a
  animação rapidamente — no projeto real, cada canal fica ativo por até 24
  horas (ver seção seguinte).

## O que o ZimaGotchi de verdade faz

O ZimaGotchi é um mascote defensivo para [ZimaOS](https://zimaspace.com/)
que reage à atividade Wi‑Fi de redes que você mesmo autorizou (pelo BSSID
delas). Ele:

- captura **somente de forma passiva** — nunca desautentica nem injeta
  quadros;
- fica restrito, no nível do filtro de captura, às redes que você listar;
- guarda PCAPs rotativos e um histórico por rede em SQLite;
- valida handshakes WPA (mensagens EAPOL 1–4) sem tentar quebrar nenhuma
  senha — ele só registra que a autenticação completa foi observada;
- tem nível, XP e humor que evoluem com o tempo ligado, a atividade das
  redes e as autenticações capturadas;
- expõe um endpoint REST para integração com Home Assistant;
- inclui watchdog para recuperar o adaptador Wi‑Fi automaticamente em caso
  de instabilidade do driver.

## Estrutura pública

```text
ZimaGotchi/
├── index.html              # demonstração do GitHub Pages, só com dados fictícios
├── README.md               # documentação pública
└── server/
    ├── app.py              # backend Python
    ├── index.html          # painel local real
    ├── networks.example.json # exemplo fictício; networks.json fica privado
    ├── Dockerfile
    └── install.sh
```

Nenhum PCAP, banco SQLite, log, IP privado, SSID ou BSSID da instalação que
originou o projeto faz parte deste repositório.

## Como baixar e instalar a versão real

Pré‑requisitos: um host com [ZimaOS](https://zimaspace.com/) e Docker, e um
adaptador Wi‑Fi compatível com modo monitor (o projeto foi validado com o
chipset RTL8188ETV / driver `rtl8xxxu`).

1. Baixe ou clone este repositório no servidor ZimaOS:

   ```bash
   git clone https://github.com/JacksonJeanPS/ZimaGotchi.git
   cd ZimaGotchi/server
   ```

   Alternativamente, use o botão **Code → Download ZIP** do GitHub.

2. Crie sua configuração privada e edite a cópia:

   ```bash
   cp networks.example.json networks.json
   nano networks.json
   ```

   Preencha o **nome e o BSSID das redes que você tem
   autorização para monitorar** (as suas próprias, ou de quem te autorizou
   explicitamente). O `.gitignore` bloqueia `networks.json`, PCAPs, bancos e
   logs para reduzir o risco de publicação acidental.
3. Dentro da pasta do projeto, rode:

   ```bash
   chmod +x install.sh
   ./install.sh
   ```

4. Acesse o painel em:

   ```text
   http://IP_DO_ZIMAOS:8686
   ```

   Para descobrir o IP do servidor:

   ```bash
   ip -4 addr show eth0
   ```

O instalador foi criado para o RTL8188ETV (`0bda:0179`) e valida esse identificador
antes de permitir o reset USB automático. Outros adaptadores podem exigir
ajustes no watchdog e no `install.sh`.

## Como o GitHub Pages desta demo é publicado

O `index.html` da raiz é exclusivamente a demonstração segura. No GitHub,
acesse **Settings → Pages**, escolha **Deploy from a branch**, branch `main` e
pasta `/ (root)`. A publicação ficará disponível em:

```text
https://jacksonjeanps.github.io/ZimaGotchi/
```

Como é um arquivo estático sem dependências, não há passo de build — o
GitHub Pages serve o `index.html` diretamente.

## Aviso de privacidade e segurança

Esta demo não representa nenhuma rede real e não deve ser confundida com o
painel ao vivo de uma instalação real. Se você publicar seu ZimaGotchi real
(o painel do endpoint `/`) publicamente na internet, lembre-se de que ele
mostra os nomes das redes autorizadas que você configurou — recomendamos
mantê-lo acessível apenas na sua rede local, não exposto publicamente.
