/**
 * OnchainBrief — SAP register_agent diagnostic
 *
 * Standalone investigation of the Anchor 3007 mystery on devnet:
 *   - register_agent fails with AccountOwnedByWrongProgram on global_registry
 *   - Left = NativeLoader1111…, Right = SAPpUh…
 *   - BUT RPC says owner = SAPpUh, lamports = 1621680, space = 105
 *
 * This script does NOT spend lamports unless --send is passed (we read first).
 *
 * Run:
 *   npm run diag           # read-only diagnostic
 *   npm run diag -- --send # also try registerAgent via direct program.methods
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
const { Keypair, PublicKey, SystemProgram, LAMPORTS_PER_SOL } = require(
  "@solana/web3.js",
);
const BN = require("bn.js");
const bs58 = require("bs58");

const SEND_MODE = process.argv.includes("--send");

function loadKeypair(): InstanceType<typeof Keypair> {
  const keypairPath = process.env.SAP_KEYPAIR_PATH;
  if (keypairPath) {
    const raw = readFileSync(keypairPath, "utf8");
    const bytes = Uint8Array.from(JSON.parse(raw));
    return Keypair.fromSecretKey(bytes);
  }
  const bs58Key = process.env.SAP_PRIVATE_KEY;
  if (bs58Key) {
    return Keypair.fromSecretKey(bs58.decode(bs58Key));
  }
  console.error("  Need SAP_KEYPAIR_PATH or SAP_PRIVATE_KEY in env.");
  process.exit(1);
}

function section(title: string) {
  console.log();
  console.log("=".repeat(70));
  console.log("  " + title);
  console.log("=".repeat(70));
}

async function main() {
  section("Setup");
  const keypair = loadKeypair();
  const sapConn = SapConnection.devnet();
  const client = sapConn.fromKeypair(keypair);
  console.log(`  Wallet         : ${keypair.publicKey.toBase58()}`);
  console.log(`  RPC            : https://api.devnet.solana.com`);
  console.log(`  program.programId: ${client.program.programId.toBase58()}`);
  console.log(`  IDL.address    : ${client.program.idl.address ?? "(none)"}`);

  section("PDA derivations");
  const [globalPda, globalBump] = deriveGlobalRegistry();
  const [agentPda, agentBump] = deriveAgent(keypair.publicKey);
  const [statsPda, statsBump] = deriveAgentStats(agentPda);
  console.log(`  globalRegistry : ${globalPda.toBase58()}  bump=${globalBump}`);
  console.log(`  agentAccount   : ${agentPda.toBase58()}  bump=${agentBump}`);
  console.log(`  agentStats     : ${statsPda.toBase58()}  bump=${statsBump}`);

  section("RPC view (raw getAccountInfo)");
  const info = await sapConn.connection.getAccountInfo(globalPda);
  if (!info) {
    console.log("  globalRegistry NOT FOUND on devnet.");
  } else {
    console.log(`  globalRegistry.owner    = ${info.owner.toBase58()}`);
    console.log(`  globalRegistry.lamports = ${info.lamports}`);
    console.log(`  globalRegistry.dataLen  = ${info.data.length}`);
    console.log(
      `  owner matches programId : ${info.owner.equals(client.program.programId)}`,
    );
  }
  const agentInfo = await sapConn.connection.getAccountInfo(agentPda);
  if (!agentInfo) {
    console.log("  agentAccount   NOT FOUND on devnet (so it's truly missing).");
  } else {
    console.log(`  agentAccount.owner      = ${agentInfo.owner.toBase58()}`);
    console.log(`  agentAccount.lamports   = ${agentInfo.lamports}`);
    console.log(`  agentAccount.dataLen    = ${agentInfo.data.length}`);
  }

  section("Anchor view (program.account fetchers)");
  try {
    const gr = await client.program.account.globalRegistry.fetch(globalPda);
    console.log(`  globalRegistry deserialized by Anchor OK.`);
    console.log(
      `    keys: ${Object.keys(gr ?? {}).slice(0, 12).join(", ")} ...`,
    );
  } catch (e: any) {
    console.log(`  globalRegistry FETCH FAILED: ${e.message?.slice(0, 200)}`);
  }
  try {
    const a = await client.program.account.agentAccount.fetchNullable(agentPda);
    console.log(`  agentAccount Anchor fetchNullable = ${a ? "exists" : "null"}`);
  } catch (e: any) {
    console.log(`  agentAccount FETCH FAILED: ${e.message?.slice(0, 200)}`);
  }
  try {
    const grViaModule = await client.agent.fetchGlobalRegistry();
    console.log(`  client.agent.fetchGlobalRegistry() OK.`);
    console.log(
      `    keys: ${Object.keys(grViaModule ?? {}).slice(0, 12).join(", ")} ...`,
    );
  } catch (e: any) {
    console.log(
      `  client.agent.fetchGlobalRegistry() FAILED: ${e.message?.slice(0, 200)}`,
    );
  }

  section("Existing agents on devnet (Anchor-discovered)");
  try {
    const all = await client.program.account.agentAccount.all();
    console.log(`  Found ${all.length} agentAccount(s) on devnet`);
    if (all.length > 0) {
      const sample = all[0];
      console.log(`  Sample agent dump:`);
      console.log(`    pda     : ${sample.publicKey.toBase58()}`);
      console.log(`    keys    : ${Object.keys(sample.account).join(", ")}`);
      const acc: any = sample.account;
      const ownerPk: any = acc.wallet ?? acc.authority ?? acc.owner ?? null;
      console.log(`    name    : ${acc.name}`);
      console.log(`    owner pk: ${ownerPk?.toBase58?.() ?? ownerPk}`);

      // Find the tx that created this agent — last sig.
      const sigs = await sapConn.connection.getSignaturesForAddress(
        sample.publicKey,
        { limit: 10 },
      );
      console.log(`    sigs    : ${sigs.length}`);
      if (sigs.length > 0) {
        const oldest = sigs[sigs.length - 1];
        console.log(`    oldest  : ${oldest.signature}`);
        const tx = await sapConn.connection.getTransaction(oldest.signature, {
          maxSupportedTransactionVersion: 0,
          commitment: "confirmed",
        });
        if (tx) {
          const msg: any = tx.transaction.message;
          const acctKeys = msg.staticAccountKeys ?? msg.accountKeys;
          console.log(`    Account keys in working registerAgent tx:`);
          for (let i = 0; i < acctKeys.length; i++) {
            console.log(`      [${i}] ${acctKeys[i].toBase58()}`);
          }
          const ixs = msg.compiledInstructions ?? msg.instructions;
          for (let i = 0; i < ixs.length; i++) {
            const ix = ixs[i];
            const acctIdx = ix.accountKeyIndexes ?? ix.accounts;
            const progIdx = ix.programIdIndex;
            const prog = acctKeys[progIdx].toBase58();
            console.log(
              `    ix ${i}: program=${prog.slice(0, 16)}…, accIdx=[${acctIdx.join(",")}]`,
            );
          }
        }
      }
    }
  } catch (e: any) {
    console.log(`  agentAccount.all() FAILED: ${e.message?.slice(0, 200)}`);
  }

  section("IDL register_agent account spec");
  const idl = client.program.idl;
  const ra = idl.instructions.find((x: any) => x.name === "register_agent");
  if (ra) {
    console.log("  IDL register_agent accounts:");
    for (const ac of ra.accounts) {
      const seedRepr = ac.pda?.seeds
        ?.map((s: any) =>
          s.kind === "const"
            ? `const(${(s.value as number[]).slice(0, 12).join(",")})`
            : s.kind === "account"
              ? `account(${s.path ?? s.account})`
              : s.kind,
        )
        .join(" | ") ?? "(no PDA seeds in IDL)";
      console.log(
        `    - ${(ac.name ?? "?").padEnd(20)} signer=${ac.signer ? "Y" : "N"} writable=${ac.writable ? "Y" : "N"} pda=${seedRepr}`,
      );
    }
  } else {
    console.log("  ! IDL has no register_agent (something's very wrong)");
  }

  if (!SEND_MODE) {
    console.log();
    console.log("  Read-only diagnostic done. Pass --send to also try the");
    console.log("  registerAgent path with explicit accounts (will spend ~0.003 SOL).");
    return;
  }

  section("Direct program.methods.registerAgent(...) with explicit accounts");
  if (agentInfo) {
    console.log(
      "  agentAccount already exists on-chain — skipping send (would double-register).",
    );
    return;
  }

  const capabilities = [
    {
      id: "onchain_event_to_brief",
      description: "End-to-end pipeline: event -> brief + visual",
      protocolId: "onchainbrief",
      version: "1.0",
    },
  ];
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
  const protocols = ["x402", "solana"];
  const name = "OnchainBriefDiag";
  const description = "diag";
  const agentUri = "https://github.com/cryptoyasenka/onchainbrief";
  const x402Endpoint = "https://facilitator.acedata.cloud/.well-known/x402";

  // Attempt 1: SDK-style camelCase
  console.log(`  Attempt 1: program.methods.registerAgent + camelCase accounts...`);
  try {
    const sig = await client.program.methods
      .registerAgent(
        name,
        description,
        capabilities,
        pricing,
        protocols,
        null,
        agentUri,
        x402Endpoint,
      )
      .accounts({
        wallet: keypair.publicKey,
        agent: agentPda,
        agentStats: statsPda,
        globalRegistry: globalPda,
        systemProgram: SystemProgram.programId,
      })
      .rpc();
    console.log(`  SUCCESS — tx: ${sig}`);
    console.log(`  https://solscan.io/tx/${sig}?cluster=devnet`);
    return;
  } catch (e: any) {
    console.log(`  FAILED: ${e.message?.slice(0, 300)}`);
    if (e.logs) {
      console.log("  Logs:");
      for (const l of e.logs.slice(0, 20)) console.log(`    ${l}`);
    }
  }

  // Attempt 2: accountsStrict + camelCase — bypass Anchor auto-resolve
  console.log();
  console.log(`  Attempt 2: accountsStrict + camelCase (no auto-resolve)...`);
  try {
    // First, build the instruction and dump account keys to verify what we send.
    const ix = await client.program.methods
      .registerAgent(
        name,
        description,
        capabilities,
        pricing,
        protocols,
        null,
        agentUri,
        x402Endpoint,
      )
      .accountsStrict({
        wallet: keypair.publicKey,
        agent: agentPda,
        agentStats: statsPda,
        globalRegistry: globalPda,
        systemProgram: SystemProgram.programId,
      })
      .instruction();
    console.log(`  Built ix with ${ix.keys.length} keys:`);
    for (let i = 0; i < ix.keys.length; i++) {
      const k = ix.keys[i];
      console.log(
        `    [${i}] ${k.pubkey.toBase58()}  signer=${k.isSigner ? "Y" : "N"} writable=${k.isWritable ? "Y" : "N"}`,
      );
    }

    const sig = await client.program.methods
      .registerAgent(
        name,
        description,
        capabilities,
        pricing,
        protocols,
        null,
        agentUri,
        x402Endpoint,
      )
      .accountsStrict({
        wallet: keypair.publicKey,
        agent: agentPda,
        agentStats: statsPda,
        globalRegistry: globalPda,
        systemProgram: SystemProgram.programId,
      })
      .rpc();
    console.log(`  SUCCESS — tx: ${sig}`);
    console.log(`  https://solscan.io/tx/${sig}?cluster=devnet`);
    return;
  } catch (e: any) {
    console.log(`  FAILED: ${e.message?.slice(0, 300)}`);
    if (e.logs) {
      console.log("  Logs:");
      for (const l of e.logs.slice(0, 20)) console.log(`    ${l}`);
    }
  }

  // Attempt 3: accountsPartial (let Anchor auto-resolve from IDL)
  console.log();
  console.log(`  Attempt 3: accountsPartial — only wallet, let Anchor auto-resolve PDAs...`);
  try {
    const sig = await client.program.methods
      .registerAgent(
        name,
        description,
        capabilities,
        pricing,
        protocols,
        null,
        agentUri,
        x402Endpoint,
      )
      .accountsPartial({
        wallet: keypair.publicKey,
      })
      .rpc();
    console.log(`  SUCCESS — tx: ${sig}`);
    console.log(`  https://solscan.io/tx/${sig}?cluster=devnet`);
    return;
  } catch (e: any) {
    console.log(`  FAILED: ${e.message?.slice(0, 300)}`);
    if (e.logs) {
      console.log("  Logs:");
      for (const l of e.logs.slice(0, 20)) console.log(`    ${l}`);
    }
  }
}

main().catch((err) => {
  console.error("Error:", err.message || err);
  if (err.logs) {
    for (const l of err.logs.slice(0, 30)) console.error("  log:", l);
  }
  process.exit(1);
});
