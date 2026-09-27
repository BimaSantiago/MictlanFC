#![no_std]
use soroban_sdk::{contract, contracterror, contractimpl, contracttype, Address, Env};

mod test;

#[contracterror]
#[derive(Copy, Clone, Debug, Eq, PartialEq, PartialOrd, Ord)]
#[repr(u32)]
pub enum Error {
    AlreadyClaimed = 1,
    InsufficientBalance = 2,
    InvalidAmount = 3,
    NotInitialized = 4,
}

#[contracttype]
pub enum DataKey {
    Admin,
    Balance(Address),
    FaucetAmount,
    Claimed(Address),
}

#[contract]
pub struct TokenContract;

#[contractimpl]
impl TokenContract {
    pub fn initialize(env: Env, admin: Address, faucet_amount: i128) -> Result<(), Error> {
        admin.require_auth();

        if faucet_amount <= 0 {
            return Err(Error::InvalidAmount);
        }
        if env.storage().instance().has(&DataKey::Admin) {
            panic!("Contract already initialized");
        }

        env.storage().instance().set(&DataKey::Admin, &admin);
        env.storage().instance().set(&DataKey::FaucetAmount, &faucet_amount);
        Ok(())
    }

    pub fn balance(env: Env, id: Address) -> i128 {
        env.storage()
            .persistent()
            .get(&DataKey::Balance(id))
            .unwrap_or(0)
    }

    pub fn transfer(env: Env, from: Address, to: Address, amount: i128) -> Result<(), Error> {
        from.require_auth();

        if amount <= 0 {
            return Err(Error::InvalidAmount);
        }

        let from_balance = Self::balance(env.clone(), from.clone());
        if from_balance < amount {
            return Err(Error::InsufficientBalance);
        }

        let to_balance = Self::balance(env.clone(), to.clone());

        env.storage()
            .persistent()
            .set(&DataKey::Balance(from.clone()), &(from_balance - amount));
        env.storage()
            .persistent()
            .set(&DataKey::Balance(to.clone()), &(to_balance + amount));

        Ok(())
    }

    pub fn faucet(env: Env, to: Address) -> Result<(), Error> {
        to.require_auth();

        let claimed_key = DataKey::Claimed(to.clone());
        let has_claimed: bool = env
            .storage()
            .persistent()
            .get(&claimed_key)
            .unwrap_or(false);

        if has_claimed {
            return Err(Error::AlreadyClaimed);
        }

        let faucet_amount: i128 = env
            .storage()
            .instance()
            .get(&DataKey::FaucetAmount)
            .ok_or(Error::NotInitialized)?;

        let current_balance = Self::balance(env.clone(), to.clone());
        
        env.storage()
            .persistent()
            .set(&DataKey::Balance(to.clone()), &(current_balance + faucet_amount));
        
        env.storage()
            .persistent()
            .set(&claimed_key, &true);

        Ok(())
    }

    pub fn get_faucet_amount(env: Env) -> Result<i128, Error> {
        env.storage()
            .instance()
            .get(&DataKey::FaucetAmount)
            .ok_or(Error::NotInitialized)
    }
}
