/**
 * OnchainBrief — SAP On-Chain Registration
 *
 * Registers the OnchainBrief agent on the Synapse Agent Protocol so
 * that any SAP-discovery client can find this agent, see its six
 * capabilities, and quote its x402 price per call.
 *
 * What this does:
 *   1. Load a dedicated Solana keypair (SAP_KEYPAIR_PATH or SAP_PRIVATE_KEY)
 *   2. Connect to devnet (default) or mainnet-beta (with --mainnet)
 *   3. (Devnet only) Top up via airdrop with retry/backoff
 *   4. Register the OnchainBrief agent on-chain with our 6 capabilities,
 *      0.001 SOL pricing, x402 settlement
 *   5. Publish a tool descriptor for each of the 6 capabilities
 *   6. Register capability + protocol discovery indexes (idempotent)
 *   7. Verify by reading our own on-chain profile and stats
 *
 * Run:
 *   npm run sap:plan       # Dry-run, no transactions
 *   npm run sap            # Devnet registration (free airdrop)
 *   npm run sap:mainnet    # Mainnet registration (~0.05 SOL real)
 *
 * The script is safe to re-run: it checks for an existing agent
 * account first and only writes the parts that are missing.
 */

import { config } from "dotenv";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
config({ path: resolve(__dirname, "..", "..", ".env") });
config();

import { createRequire } from "module";
const require = createRequire(import.meta.url);

const {
  SapConnection,
  TokenType,
  SettlementMode,
  deriveAgent,
  deriveAgentStats,
  deriveGlobalRegistry,
} = require("@oobe-protocol-labs/synapse-sap-sdk");
const {
  Keypair,
  LAMPORTS_PER_SOL,
  PublicKey,
  SystemProgram,
} = require("@solana/web3.js");
const BN = require("bn.js");
const bs58 = require("bs58");

/**
 * Derive the AgentPricingMenu PDA — seeds = ["sap_pricing", agentPda].
 *
 * On-chain `register_agent` expects this account at index 3 in the
 * instruction's account list, but the bundled SDK IDL (through 0.17.x)
 * doesn't declare it for that instruction. We supply it manually below.
 */
function derivePricingMenu(
  agentPda: InstanceType<typeof PublicKey>,
  programId: InstanceType<typeof PublicKey>,
): [InstanceType<typeof PublicKey>, number] {
  return PublicKey.findProgramAddressSync(
    [Buffer.from("sap_pricing"), agentPda.toBuffer()],
    programId,
  );
}

const PLAN_MODE = process.argv.includes("--plan");
const MAINNET_MODE = process.argv.includes("--mainnet");
const NETWORK_LABEL = MAINNET_MODE ? "mainnet-beta" : "devnet";

const AGENT_NAME = "OnchainBrief";
const AGENT_DESCRIPTION = "Solana on-chain events -> sourced briefs + visuals";
const AGENT_URI = "https://github.com/cryptoyasenka/onchainbrief";
const X402_ENDPOINT = "https://facilitator.acedata.cloud/.well-known/x402";
const PROTOCOL_ID = "onchainbrief";
const PROTOCOLS = ["x402", "solana"];

// OnchainBrief capability catalog — these mirror the runtime pipeline
// 1:1, so that on-chain discovery reflects what the agent actually does.
// Capability IDs use the on-chain `<protocol>:<kebab-name>` convention
// (matches SURVIVOR / SynapseScout etc. on devnet). The on-chain validator
// rejects underscore_snake_case as `InvalidCapabilityFormat (6026)`.
//
// Tool `name` is limited to <=32 chars on-chain (tools.rs:79
// `ToolNameTooLong (6048)`), so we use a short `ob-` prefix there while
// keeping the longer `onchainbrief:` form for capability IDs (which use
// a separate, more lenient validator).
const CAPABILITIES = [
  {
    name: "ob-event-to-brief",
    capability: "onchainbrief:event-to-brief",
    description: "End-to-end pipeline: event -> brief + visual",
    paramsCount: 1,
    requiredParams: 1,
  },
  {
    name: "ob-solana-event-watch",
    capability: "onchainbrief:solana-event-watch",
    description: "Solana logs watch + local triage",
    paramsCount: 2,
    requiredParams: 1,
  },
  {
    name: "ob-web-context-research",
    capability: "onchainbrief:web-context-research",
    description: "Google SERP context (ACE Serp)",
    paramsCount: 3,
    requiredParams: 1,
  },
  {
    name: "ob-event-narrative",
    capability: "onchainbrief:event-narrative",
    description: "Factual narrative (ACE Chat)",
    paramsCount: 4,
    requiredParams: 2,
  },
  {
    name: "ob-trading-card-render",
    capability: "onchainbrief:trading-card-render",
    description: "Branded card visual (ACE Image)",
    paramsCount: 3,
    requiredParams: 1,
  },
  {
    name: "ob-artifact-attest",
    capability: "onchainbrief:artifact-onchain-attest",
    description: "Memo-program attestation of artifact",
    paramsCount: 2,
    requiredParams: 2,
  },
];

async function ensureGlobalRegistry(
  client: any,
  connection: any,
  keypair: InstanceType<typeof Keypair>,
): Promise<void> {
  const [globalPda] = deriveGlobalRegistry();
  const info = await connection.getAccountInfo(globalPda);
  if (info && info.owner.equals(client.program.programId)) {
    console.log(
      `         GlobalRegistry already initialized at ${globalPda.toBase58()}`,
    );
    return;
  }
  console.log(
    `         GlobalRegistry not initialized; bootstrapping at ${globalPda.toBase58()}`,
  );
  const sig = await client.program.methods
    .initializeGlobal()
    .accounts({ authority: keypair.publicKey })
    .rpc();
  console.log(`         GlobalRegistry init tx: ${sig}`);
}

function loadKeypair(): InstanceType<typeof Keypair> {
  const keypairPath = process.env.SAP_KEYPAIR_PATH;
  if (keypairPath) {
    const raw = readFileSync(keypairPath, "utf8");
    const bytes = Uint8Array.from(JSON.parse(raw));
    return Keypair.fromSecretKey(bytes);
  }
  const bs58Key = process.env.SAP_PRIVATE_KEY;
  if (bs58Key) {
    const decoded = bs58.decode(bs58Key);
    return Keypair.fromSecretKey(decoded);
  }
  console.error(
    "  Error: provide SAP_KEYPAIR_PATH (Solana CLI JSON byte-array)",
  );
  console.error(
    "         or SAP_PRIVATE_KEY (base58 secret key). This script does",
  );
  console.error("         not generate keys.");
  process.exit(1);
}

async function main() {
  console.log("=".repeat(70));
  console.log("  OnchainBrief — SAP On-Chain Registration");
  console.log(`  Register OnchainBrief agent on Solana (${NETWORK_LABEL})`);
  if (PLAN_MODE)
    console.log("  MODE: Plan (dry-run) — no transactions will be sent");
  if (MAINNET_MODE) console.log("  MODE: MAINNET — real SOL will be spent!");
  console.log("=".repeat(70));
  console.log();

  // ─── Step 1: Load dedicated keypair ───
  const keypair = loadKeypair();
  console.log("  [1/7] Loaded keypair");
  console.log(`         Public key: ${keypair.publicKey.toBase58()}`);
  console.log();

  // ─── Step 2: Connect to network ───
  const sapConn = MAINNET_MODE
    ? SapConnection.mainnet()
    : SapConnection.devnet();
  const client = sapConn.fromKeypair(keypair);
  console.log(`  [2/7] Connected to Solana ${NETWORK_LABEL}`);
  console.log(`         Program: SAPpUhsWLJG1FfkGRcXagEDMrMsWGjbky7AyhGpFETZ`);
  console.log();

  const [agentPda] = deriveAgent(keypair.publicKey);

  if (PLAN_MODE) {
    console.log("  [3/7] Airdrop: SKIPPED (plan mode)");
    console.log();
    console.log("  [4/7] Agent Registration Plan:");
    console.log("  " + "-".repeat(66));
    console.log(`  Name:          ${AGENT_NAME}`);
    console.log(`  Description:   ${AGENT_DESCRIPTION}`);
    console.log(`  Agent URI:     ${AGENT_URI}`);
    console.log(`  x402 Endpoint: ${X402_ENDPOINT}`);
    console.log(`  Agent PDA:     ${agentPda.toBase58()}`);
    console.log(`  Protocols:     ${PROTOCOLS.join(", ")}`);
    console.log();
    console.log(`  Capabilities (${CAPABILITIES.length}):`);
    for (const c of CAPABILITIES) {
      console.log(`    - ${c.capability.padEnd(26)} — ${c.description}`);
    }
    console.log();
    console.log("  Pricing:");
    console.log(`    Tier:        standard`);
    console.log(`    Price:       0.001 SOL per call (BN 1_000_000)`);
    console.log(`    Token:       SOL`);
    console.log(`    Settlement:  x402`);
    console.log(`    Rate limit:  60 calls/min`);
    console.log(`    Max calls:   100,000 per epoch`);
    console.log();

    console.log(`  [5/7] Tool Descriptors (${CAPABILITIES.length}):`);
    for (const c of CAPABILITIES) {
      console.log(
        `    - ${c.name.padEnd(26)} POST  params:${c.paramsCount} required:${c.requiredParams}`,
      );
    }
    console.log();

    console.log("  [6/7] Discovery Indexes:");
    console.log("  Capability indexes:");
    for (const c of CAPABILITIES) {
      console.log(`    - ${c.capability}`);
    }
    console.log("  Protocol indexes:");
    for (const p of PROTOCOLS) {
      console.log(`    - ${p}`);
    }
    console.log();

    console.log("  [7/7] Verification: SKIPPED (plan mode)");
    console.log();

    console.log("  " + "=".repeat(66));
    console.log("  On-Chain Transactions Summary (will execute in live mode):");
    console.log("  " + "-".repeat(66));
    console.log(
      `  TX 0:        initializeGlobal — only if SAP singleton missing`,
    );
    console.log(`               (mainnet: always already initialized; devnet:`);
    console.log(`               bootstrapped by us if absent)`);
    console.log(`  TX 1:        registerAgent — create ${AGENT_NAME}`);
    for (let i = 0; i < CAPABILITIES.length; i++) {
      console.log(
        `  TX ${i + 2}:        publishTool — ${CAPABILITIES[i].name}`,
      );
    }
    const baseTx = CAPABILITIES.length + 2;
    for (let i = 0; i < CAPABILITIES.length; i++) {
      console.log(
        `  TX ${baseTx + i}+:       initCapabilityIndex + addToCapabilityIndex — ${CAPABILITIES[i].capability}`,
      );
    }
    for (let i = 0; i < PROTOCOLS.length; i++) {
      console.log(
        `  TX ${baseTx + CAPABILITIES.length + i}+:       initProtocolIndex + addToProtocolIndex — ${PROTOCOLS[i]}`,
      );
    }
    console.log();
    const txCount =
      1 + CAPABILITIES.length + CAPABILITIES.length + PROTOCOLS.length;
    console.log(`  Estimated cost: ~0.05 SOL (transaction fees)`);
    console.log(`  Total transactions: ~${txCount} (lower bound, no retries)`);
    console.log();
    console.log("  To execute for real:");
    console.log("    1. Devnet: free airdrop happens inside this script");
    console.log("       Mainnet: send ~0.05 SOL to the public key above");
    console.log("    2. Run:   npm run sap            # devnet");
    console.log("       Or:    npm run sap:mainnet    # mainnet-beta");
    console.log("  " + "=".repeat(66));
    console.log();
    return;
  }

  // ─── Step 3: Fund wallet ───
  if (MAINNET_MODE) {
    console.log(
      "  [3/7] Checking mainnet SOL balance (no airdrop on mainnet)...",
    );
  } else {
    console.log("  [3/7] Requesting devnet SOL airdrop...");
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        const sig = await sapConn.connection.requestAirdrop(
          keypair.publicKey,
          1 * LAMPORTS_PER_SOL,
        );
        await sapConn.connection.confirmTransaction(sig, "confirmed");
        console.log(`         Received 1 SOL (attempt ${attempt})`);
        break;
      } catch (e: any) {
        if (attempt === 3) {
          console.log(
            `         Airdrop failed after ${attempt} attempts: ${e.message?.slice(0, 80)}`,
          );
          console.log("         Checking existing balance...");
        } else {
          console.log(
            `         Attempt ${attempt} rate-limited, retrying in 5s...`,
          );
          await new Promise((r) => setTimeout(r, 5000));
        }
      }
    }
  }

  const balance = await sapConn.connection.getBalance(keypair.publicKey);
  console.log(
    `         Balance: ${(balance / LAMPORTS_PER_SOL).toFixed(4)} SOL`,
  );
  if (balance < 0.05 * LAMPORTS_PER_SOL) {
    if (MAINNET_MODE) {
      console.error(
        `         Insufficient SOL on mainnet. Send at least 0.05 SOL to:`,
      );
      console.error(`         ${keypair.publicKey.toBase58()}`);
    } else {
      console.error(
        "         Insufficient SOL. Devnet rate limit hit; either wait",
      );
      console.error(
        "         ~8h for the public faucet reset, or top up via a",
      );
      console.error(
        "         secondary faucet (e.g. faucet.quicknode.com/solana/devnet)",
      );
      console.error(`         and re-run. Target: ${keypair.publicKey.toBase58()}`);
    }
    process.exit(1);
  }
  console.log();

  // ─── Step 3b: Ensure SAP GlobalRegistry singleton exists ───
  // The SAP program needs its singleton initialised before any agent can
  // register. On mainnet this is always done already; on a fresh devnet
  // deployment the singleton can be missing — in that case we bootstrap
  // it ourselves (anyone can call `initialize_global` per the IDL).
  console.log("         Checking SAP GlobalRegistry singleton...");
  await ensureGlobalRegistry(client, sapConn.connection, keypair);
  console.log();

  // ─── Step 4: Register OnchainBrief agent on-chain ───
  console.log(`  [4/7] Registering ${AGENT_NAME} agent on-chain...`);

  const existingAgent = await client.agent.fetchNullable();
  if (existingAgent) {
    console.log(`         Agent already registered. Skipping register step.`);
    console.log(`         Name: ${existingAgent.name}`);
    console.log(`         Agent PDA: ${agentPda.toBase58()}`);
  } else {
    const capabilities = CAPABILITIES.map((c) => ({
      id: c.capability,
      description: c.description,
      protocolId: PROTOCOL_ID,
      version: "1.0",
    }));

    const pricing = [
      {
        tierId: "standard",
        pricePerCall: new BN(1_000_000),
        minPricePerCall: null,
        maxPricePerCall: null,
        rateLimit: 60,
        maxCallsPerSession: 100_000,
        burstLimit: null,
        tokenType: TokenType.Sol,
        tokenMint: null,
        tokenDecimals: 9,
        settlementMode: SettlementMode.X402,
        minEscrowDeposit: null,
        batchIntervalSec: null,
        volumeCurve: null,
      },
    ];

    const [statsPda] = deriveAgentStats(agentPda);
    const [globalPda] = deriveGlobalRegistry();
    const [pricingPda] = derivePricingMenu(agentPda, client.program.programId);

    const ix = await client.program.methods
      .registerAgent(
        AGENT_NAME,
        AGENT_DESCRIPTION,
        capabilities,
        pricing,
        PROTOCOLS,
        null,
        AGENT_URI,
        X402_ENDPOINT,
      )
      .accountsStrict({
        wallet: keypair.publicKey,
        agent: agentPda,
        agentStats: statsPda,
        globalRegistry: globalPda,
        systemProgram: SystemProgram.programId,
      })
      .instruction();

    ix.keys.splice(3, 0, {
      pubkey: pricingPda,
      isSigner: false,
      isWritable: true,
    });

    const provider = client.program.provider;
    const { Transaction } = require("@solana/web3.js");
    const tx = new Transaction().add(ix);
    const registerTx = await provider.sendAndConfirm(tx, [keypair], {
      commitment: "confirmed",
    });

    console.log(`         TX: ${registerTx}`);
    console.log(`         Agent registered on-chain.`);
    console.log(`         Agent PDA: ${agentPda.toBase58()}`);
    console.log(`         PricingMenu PDA: ${pricingPda.toBase58()}`);
  }
  console.log();

  // ─── Step 5: Publish tool descriptors ───
  console.log(`  [5/7] Publishing ${CAPABILITIES.length} tool descriptors...`);
  for (const cap of CAPABILITIES) {
    try {
      const tx = await client.tools.publishByName(
        cap.name,
        PROTOCOL_ID,
        cap.description,
        JSON.stringify({ input: "object" }),
        JSON.stringify({ result: "object" }),
        1,
        0,
        cap.paramsCount,
        cap.requiredParams,
        false,
      );
      console.log(`         + ${cap.name} (tx: ${tx.slice(0, 16)}...)`);
    } catch (e: any) {
      const msg = e.message || String(e);
      const logs: string[] = e.logs || [];
      const alreadyInUse = logs.some((l: string) => l.includes("already in use"));
      if (alreadyInUse) {
        console.log(`         = ${cap.name} (already published, skipping)`);
      } else {
        const tail = logs.length ? "\n           " + logs.slice(0, 6).join("\n           ") : "";
        console.log(`         ! ${cap.name}: ${msg.slice(0, 200)}${tail}`);
      }
    }
    if (MAINNET_MODE) await new Promise((r) => setTimeout(r, 2000));
  }
  console.log();

  // ─── Step 6: Register discovery indexes ───
  console.log("  [6/7] Registering discovery indexes...");

  for (const cap of CAPABILITIES) {
    try {
      await client.indexing.initCapabilityIndex(cap.capability);
      console.log(`         + capability: ${cap.capability}`);
    } catch (e: any) {
      try {
        await client.indexing.addToCapabilityIndex(cap.capability);
        console.log(
          `         + capability: ${cap.capability} (joined existing)`,
        );
      } catch {
        console.log(
          `         ! capability: ${cap.capability}: ${e.message?.slice(0, 60)}`,
        );
      }
    }
    if (MAINNET_MODE) await new Promise((r) => setTimeout(r, 2000));
  }

  for (const protocol of PROTOCOLS) {
    try {
      await client.indexing.initProtocolIndex(protocol);
      console.log(`         + protocol: ${protocol}`);
    } catch (e: any) {
      try {
        await client.indexing.addToProtocolIndex(protocol);
        console.log(`         + protocol: ${protocol} (joined existing)`);
      } catch {
        console.log(
          `         ! protocol: ${protocol}: ${e.message?.slice(0, 60)}`,
        );
      }
    }
    if (MAINNET_MODE) await new Promise((r) => setTimeout(r, 2000));
  }
  console.log();

  // ─── Step 7: Verify by reading our own profile ───
  console.log("  [7/7] Verifying on-chain registration...");
  try {
    const [statsPda] = deriveAgentStats(agentPda);
    const agent = await client.program.account.agentAccount.fetch(agentPda);
    const stats = await client.program.account.agentStats.fetch(statsPda);

    console.log();
    console.log("  On-Chain Agent Profile:");
    console.log("  " + "-".repeat(66));
    console.log(`  Name:          ${agent.name}`);
    console.log(`  Description:   ${agent.description}`);
    console.log(`  URI:           ${agent.agentUri}`);
    console.log(`  x402 Endpoint: ${agent.x402Endpoint}`);
    console.log(`  Owner:         ${agent.wallet.toBase58()}`);
    console.log(`  Active:        ${agent.isActive}`);
    console.log(`  Capabilities:  ${agent.capabilities?.length || 0}`);
    console.log(`  Protocols:     ${(agent.protocols || []).join(", ")}`);
    console.log(`  Total Calls:   ${stats.totalCallsServed?.toString() || "0"}`);
    console.log();

    const expectedCapCount = CAPABILITIES.length;
    const actualCapCount = agent.capabilities?.length || 0;
    if (actualCapCount !== expectedCapCount) {
      console.log(
        `  WARNING: expected ${expectedCapCount} capabilities, found ${actualCapCount}`,
      );
    }

    const clusterParam = MAINNET_MODE ? "" : "?cluster=devnet";
    console.log(`  Solana Explorer:`);
    console.log(
      `  https://explorer.solana.com/address/${agentPda.toBase58()}${clusterParam}`,
    );
  } catch (e: any) {
    console.log(`  Could not fetch profile: ${e.message?.slice(0, 80)}`);
  }

  console.log();
  console.log("  " + "=".repeat(66));
  console.log("  Done.");
  console.log("  " + "=".repeat(66));
  console.log();
}

main().catch((err) => {
  console.error("Error:", err.message || err);
  process.exit(1);
});
