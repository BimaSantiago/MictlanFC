#![cfg(test)]

use super::*;
use soroban_sdk::testutils::Address as _;
use soroban_sdk::Env;

fn setup() -> (Env, TokenContractClient<'static>, Address) {
    let env = Env::default();
    env.mock_all_auths();

    let contract_id = env.register(TokenContract, ());
    let client = TokenContractClient::new(&env, &contract_id);

    let admin = Address::generate(&env);
    client.initialize(&admin, &100);

    (env, client, admin)
}

#[test]
fn test_initialize_sets_faucet_amount() {
    let (_, client, _) = setup();
    assert_eq!(client.get_faucet_amount(), 100);
}

#[test]
fn test_initialize_twice_panics() {
    let (env, client, _) = setup();
    let other_admin = Address::generate(&env);
    let result = client.try_initialize(&other_admin, &50);
    assert!(result.is_err());
}

#[test]
fn test_faucet_credits_balance_once() {
    let (env, client, _) = setup();
    let user = Address::generate(&env);

    assert_eq!(client.balance(&user), 0);

    client.faucet(&user);
    assert_eq!(client.balance(&user), 100);

    let result = client.try_faucet(&user);
    assert_eq!(result, Err(Ok(Error::AlreadyClaimed)));
}

#[test]
fn test_transfer_moves_balance() {
    let (env, client, _) = setup();
    let sender = Address::generate(&env);
    let receiver = Address::generate(&env);

    client.faucet(&sender);
    client.transfer(&sender, &receiver, &40);

    assert_eq!(client.balance(&sender), 60);
    assert_eq!(client.balance(&receiver), 40);
}

#[test]
fn test_transfer_insufficient_balance_fails() {
    let (env, client, _) = setup();
    let sender = Address::generate(&env);
    let receiver = Address::generate(&env);

    let result = client.try_transfer(&sender, &receiver, &10);
    assert_eq!(result, Err(Ok(Error::InsufficientBalance)));
}
