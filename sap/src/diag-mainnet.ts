/**
 * OnchainBrief — mainnet register_agent SIMULATION diagnostic
 *
 * Builds the identical registerAgent instruction that register.ts sends
 * (accountsStrict + manual pricingMenu splice at index 3) and runs it
 * through connection.simulateTransaction against mainnet. Simulation does
 * NOT spend SOL, so this is safe to run repeatedly. The goal is to read
 * the Anchor program logs and see exactly which account it reports as
 * uninitialized (the 0xbc4 / 3012 AccountNotInitialized failure).
 *
 * Run:
 *   SAP_KEYPAIR_PATH=... SOLANA_RPC_URL=https://solana-rpc.publicnode.com \
 *     npx tsx src/diag-mainnet.ts
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
const { Keypair, PublicKey, SystemProgram, Transaction } = require(
  "@solana/web3.js",
);
const BN = require("bn.js");
const bs58 = require("bs58");

const RPC = process.env.SOLANA_RPC_URL || "https://solana-rpc.publicnode.com";

function loadKeypair(): InstanceType<typeof Keypair> {
  const keypairPath = process.env.SAP_KEYPAIR_PATH;
  if (keypairPath) {
    const raw = readFileSync(keypairPath, "utf8");
    return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(raw)));
  }
  const bs58Key = process.env.SAP_PRIVATE_KEY;
  if (bs58Key) return Keypair.fromSecretKey(bs58.decode(bs58Key));
  console.error("  Need SAP_KEYPAIR_PATH or SAP_PRIVATE_KEY in env.");
  process.exit(1);
}

function derivePricingMenu(
  agentPda: InstanceType<typeof PublicKey>,
  programId: InstanceType<typeof PublicKey>,
): [InstanceType<typeof PublicKey>, number] {
  return PublicKey.findProgramAddressSync(
    [Buffer.from("sap_pricing"), agentPda.toBuffer()],
    programId,
  );
}

function section(t: string) {
  console.log();
  console.log("=".repeat(70));
  console.log("  " + t);
  console.log("=".repeat(70));
}

async function main() {
  section("Setup (MAINNET simulation)");
  const keypair = loadKeypair();
  const sapConn = SapConnection.mainnet(RPC);
  const client = sapConn.fromKeypair(keypair);
  const conn = sapConn.connection;
  console.log(`  Wallet          : ${keypair.publicKey.toBase58()}`);
  console.log(`  RPC             : ${RPC}`);
  console.log(`  program.programId: ${client.program.programId.toBase58()}`);

  section("PDA derivations + on-chain existence");
  const [globalPda, globalBump] = deriveGlobalRegistry();
  const [agentPda, agentBump] = deriveAgent(keypair.publicKey);
  const [statsPda, statsBump] = deriveAgentStats(agentPda);
  const [pricingPda, pricingBump] = derivePricingMenu(
    agentPda,
    client.program.programId,
  );
  const pdas: Array<[string, any, number]> = [
    ["globalRegistry", globalPda, globalBump],
    ["agentAccount", agentPda, agentBump],
    ["agentStats", statsPda, statsBump],
    ["pricingMenu", pricingPda, pricingBump],
  ];
  for (const [label, pk, bump] of pdas) {
    const info = await conn.getAccountInfo(pk);
    const ownedBySap = info
      ? info.owner.equals(client.program.programId)
      : false;
    console.log(
      `  ${label.padEnd(15)}: ${pk.toBase58()}  bump=${bump}` +
        (info
          ? `  EXISTS owner=${info.owner.toBase58().slice(0, 8)}… sapOwned=${ownedBySap} len=${info.data.length}`
          : `  MISSING`),
    );
  }

  section("Building registerAgent ix (identical to register.ts)");
  const capabilities = [
    {
      id: "onchainbrief:event-to-brief",
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

  const ix = await client.program.methods
    .registerAgent(
      "OnchainBrief",
      "Solana on-chain events -> sourced briefs + visuals",
      capabilities,
      pricing,
      ["x402", "solana"],
      null,
      "https://github.com/cryptoyasenka/onchainbrief",
      "https://facilitator.acedata.cloud/.well-known/x402",
    )
    .accountsStrict({
      wallet: keypair.publicKey,
      agent: agentPda,
      agentStats: statsPda,
      globalRegistry: globalPda,
      systemProgram: SystemProgram.programId,
    })
    .instruction();

  console.log(`  ix keys BEFORE splice (${ix.keys.length}):`);
  for (let i = 0; i < ix.keys.length; i++) {
    const k = ix.keys[i];
    console.log(
      `    [${i}] ${k.pubkey.toBase58()}  s=${k.isSigner ? "Y" : "N"} w=${k.isWritable ? "Y" : "N"}`,
    );
  }

  async function simulate(label: string, keys: any[]) {
    section(`Simulate ${label} (no SOL spent)`);
    console.log(`  ix keys (${keys.length}):`);
    for (let i = 0; i < keys.length; i++) {
      const k = keys[i];
      console.log(
        `    [${i}] ${k.pubkey.toBase58()}  s=${k.isSigner ? "Y" : "N"} w=${k.isWritable ? "Y" : "N"}`,
      );
    }
    const variantIx = { ...ix, keys };
    const tx = new Transaction().add(variantIx);
    tx.feePayer = keypair.publicKey;
    const { blockhash } = await conn.getLatestBlockhash("confirmed");
    tx.recentBlockhash = blockhash;
    tx.sign(keypair);
    const sim = await conn.simulateTransaction(tx);
    console.log(`  err  : ${JSON.stringify(sim.value.err)}`);
    console.log(`  units: ${sim.value.unitsConsumed ?? "n/a"}`);
    console.log(`  logs :`);
    for (const l of sim.value.logs || []) console.log(`    ${l}`);
  }

  // Variant A: exactly as the IDL declares (5 accounts, no splice).
  await simulate("A: IDL accounts (no splice)", [...ix.keys]);

  // Variant B: register.ts current behaviour (pricingMenu spliced at [3]).
  const splicedKeys = [...ix.keys];
  splicedKeys.splice(3, 0, {
    pubkey: pricingPda,
    isSigner: false,
    isWritable: true,
  });
  await simulate("B: pricingMenu spliced at [3] (current register.ts)", splicedKeys);

  section("IDL register_agent account spec (for comparison)");
  const idl = client.program.idl;
  const ra = idl.instructions.find(
    (x: any) => x.name === "register_agent" || x.name === "registerAgent",
  );
  if (ra) {
    ra.accounts.forEach((ac: any, i: number) => {
      console.log(
        `    [${i}] ${(ac.name ?? "?").padEnd(18)} signer=${ac.signer ? "Y" : "N"} writable=${ac.writable ? "Y" : "N"}`,
      );
    });
  } else {
    console.log("  ! no register_agent in IDL");
  }
}

main().catch((err) => {
  console.error("Error:", err.message || err);
  if (err.logs) for (const l of err.logs.slice(0, 30)) console.error("  log:", l);
  process.exit(1);
});
