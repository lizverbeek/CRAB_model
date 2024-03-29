# !/usr/bin/env python3.11
# -*- coding: utf-8 -*-

"""

@author: lizverbeek, TabernaA

The model class for the Climate-economy Regional Agent-Based (CRAB) model).
This class is based on the MESA Model class.

"""

import itertools
import numpy as np
import pandas as pd
import networkx as nx

from collections import defaultdict

from mesa import Model
from mesa.time import BaseScheduler
from mesa import DataCollector

from CRAB_agents import *
from government import Government
from schedule import StagedActivationByType
from datacollection import model_vars, agent_vars


# -- INITIALIZE REGION SIZES -- #
N_REGIONS = 1                                      # Number of regions
REGIONS = range(N_REGIONS)
N_HOUSEHOLDS = {REGIONS[0]: 10000}                 # Number of households per region
N_FIRMS = {REGIONS[0]: {CapitalFirm: 125,          # Number of firms per type per region
                        ConsumptionGoodFirm: 200,
                        ServiceFirm: 300}}

# -- FIRM INITIALIZATION ATTRIBUTES -- #
INIT_NET_WORTH = {CapitalFirm: 150,         # Initial net worth
                  ConsumptionGoodFirm: 20,
                  ServiceFirm: 50}
INIT_CAP_AMOUNT = {CapitalFirm: 3,          # Initial capital per machine
                   ConsumptionGoodFirm: 2,
                   ServiceFirm: 2}
INIT_N_MACHINES = {CapitalFirm: 20,         # Initial number of machines
                   ConsumptionGoodFirm: 10,
                   ServiceFirm: 15}

# -- FLOOD ATTRIBUTES -- #
FLOOD_WHEN = {40: 1000, 80: 100}  # Flood occurrence (timestep: return period)
# -- ADAPTATION ATTRIBUTES -- #
AVG_HH_CONNECTIONS = 7

class CRAB_Model(Model):
    """Model class for the CRAB model. """

    def __init__(self, random_seed: int, HH_attributes: pd.DataFrame,
                 firm_flood_depths: pd.DataFrame, PMT_weights: pd.DataFrame,
                 CCA: bool=True, social_net: bool=True) -> None:
        """Initialization of the CRAB model.

        Args:
            random_seed         : Random seed for model 
            HH_attributes       : Household attributes from synthetic population file
            firm_flood_depths   : Firm buildings flood depths per return period
            PMT_weights         : Weights for household CCA decision-making
            CCA                 : Boolean (climate change adaptation on/off)
            social_net          : Boolean (social network on/off)
        """
        super().__init__()

        # -- SAVE NUMBER OF REGIONS -- #
        self.n_regions = N_REGIONS

        # -- INITIALIZE SCHEDULER -- #
        # Initialize StagedActivation scheduler
        stages = ["stage1", "stage2", "stage3", "stage4",
                  "stage5", "stage6", "stage7", "stage8"]
        self.schedule = StagedActivationByType(self, stage_list=stages)

        # --- REGULATE STOCHASTICITY --- #
        # Regulate stochasticity with numpy random generator for each agent type and region
        self.RNGs = {}  
        # Create random generator per region per agent type
        agent_types = list(N_FIRMS[0].keys()) + [Household] + [Government]
        for i, agent_class in enumerate(agent_types):
            self.RNGs[agent_class] = np.random.default_rng(random_seed + i)
        # Create separate random generator for household adaptation process
        self.RNGs["Adaptation"] = np.random.default_rng(random_seed + i + 1)
        # ------------------------------

        # -- FLOOD and ADAPTATION ATTRIBUTES -- #
        self.CCA = CCA  # True/False (adaptation on/off)
        if self.CCA:
            self.social_net = social_net
            self.PMT_weights = PMT_weights

        # -- INITIALIZE AGENTS -- #
        self.governments = defaultdict(list)
        self.firms = defaultdict(list)
        self.households = defaultdict(list)
        # Add households and firms per region
        for region in REGIONS:
            # -- CREATE FIRMS -- #
            self.firms[region] = {}
            for firm_type, N in N_FIRMS[region].items():
                self.firms[region][firm_type] = []
                # Take random subsample from synthetic population
                idx = self.RNGs[firm_type].choice(firm_flood_depths.index, N)
                for _, flood_depths in firm_flood_depths.loc[idx].iterrows():
                    flood_depths = {int(RP.lstrip("Flood depth RP")): depth
                                    for RP, depth in flood_depths.items()}
                    self.add_firm(firm_type, region=region,
                                  flood_depths=flood_depths,
                                  market_share=1/N_FIRMS[region][firm_type],
                                  net_worth=INIT_NET_WORTH[firm_type],
                                  init_n_machines=INIT_N_MACHINES[firm_type],
                                  init_cap_amount=INIT_CAP_AMOUNT[firm_type])
            
            # -- CREATE HOUSEHOLDS -- #
            self.households[region] = []
            # Take random subsample from synthetic population
            idx = self.RNGs[Household].choice(HH_attributes.index,
                                              N_HOUSEHOLDS[region])
            for _, attributes in HH_attributes.loc[idx].iterrows():
                self.add_household(region, attributes)
            # -- SOCIAL NETWORK -- #

            self.G = nx.watts_strogatz_graph(n=N_HOUSEHOLDS[region],
                                             k=AVG_HH_CONNECTIONS, p=0)
            # Relabel nodes for consistency with agent IDs
            self.G = nx.relabel_nodes(self.G, lambda x: x +
                                      sum(N_FIRMS[region].values()) + 1)
            
            # -- CREATE GOVERNMENT -- #
            self.add_government(region)

        # -- CONNECT SUPPLIERS TO CAPITAL FIRMS -- #
        # NOTE: cannot be done during initialization of firms, since
        #       CapitalFirms have to be connected to themselves.
        for region in REGIONS:
            # NOTE: assumes suppliers within same region.
            cap_firms = self.get_firms_by_type(CapitalFirm, region)
            for firm in cap_firms:
                # Get supplier
                firm.supplier = self.RNGs[type(firm)].choice(cap_firms)
                # Append brochure to offers
                firm.offers = {firm.supplier: firm.supplier.brochure}
                # Also keep track of clients on supplier side
                firm.supplier.clients.append(firm)

        # -- FIRM ATTRIBUTES (per region) -- #
        # Keep track of firms leaving and entering
        self.firm_subsidiaries = defaultdict(list)
        self.firms_to_remove = defaultdict(list)

        # -- DATACOLLECTION -- #
        self.datacollector = DataCollector(model_reporters=model_vars,
                                           agent_reporters=agent_vars)
        # ----------------------------

    def add_government(self, region: int) -> None:
        """Add government to the specified region of the CRAB model. """
        gov = Government(self, region)
        self.governments[region] = gov
        self.schedule.add(gov)

    def add_household(self, region: int, attributes: pd.DataFrame) -> None:
        """Add new household to the CRAB model and scheduler. """
        hh = Household(self, region, attributes)
        self.households[region].append(hh)
        self.schedule.add(hh)

    def add_firm(self, firm_class: type, region: int, **kwargs) -> Type[Firm]:
        """Add a firm to the CRAB model and scheduler.
        
        Args:
            firm_class          : Type of firm
            region              : Region to add firm to
            **market_share      : 
        """
        firm = firm_class(model=self, region=region, **kwargs)
        self.firms[region][firm_class].append(firm)
        self.schedule.add(firm)
        return firm

    def add_subsidiary(self, firm: Type[Firm]) -> None:
        """Create subsidiary of given firm.

        Args:
            firm        : Firm object to create subsidiary of
        """
        gov = self.governments[firm.region]

        # Initialize net worth as fraction of average net worth
        fraction_wealth = (0.9 - 0.1) * self.RNGs[type(firm)].uniform() + 0.1
        net_worth = max(gov.avg_net_worth, 1) * fraction_wealth
        # Get capital amount for new firms from government
        capital_amount = round(gov.capital_new_firm[type(firm)] * firm.cap_out_ratio)
        # Get best regional capital firm (and its brochure)
        best_cap = gov.get_best_cap()
        brochure = best_cap.brochure
        
        if isinstance(firm, CapitalFirm):
            # Initialize productivity as fraction of regional top productivity
            x_low, x_up, a, b = (-0.075, 0.075, 2, 4)
            fraction_prod = 1 + x_low + self.RNGs[type(firm)].beta(a, b) * (x_up - x_low)
            old_prod = np.around(gov.top_prod * fraction_prod, 3)
            prod = brochure["prod"]
            # Initialize market share as fraction of total at beginning
            market_share = 1 / N_FIRMS[firm.region][CapitalFirm]
            # Create new firm
            sub = type(firm)(model=self, region=firm.region,
                             flood_depths=firm.flood_depths,
                             market_share=market_share, net_worth=net_worth,
                             init_n_machines=1, init_cap_amount=capital_amount,
                             sales=capital_amount, wage=firm.wage, price=firm.price,
                             prod=prod, old_prod=old_prod, lifetime=0)
        elif isinstance(firm, ConsumptionFirm):
            # Initialize productivity as productivity of best supplier
            prod = brochure["prod"]
            # Create new firm
            sub = type(firm)(model=self, region=firm.region,
                             flood_depths=firm.flood_depths, market_share=0,
                             init_n_machines=1, init_cap_amount=capital_amount,
                             net_worth=net_worth,
                             sales=0, wage=firm.wage, price=firm.price, prod=prod,
                             lifetime=0)
            # Initialze competitiveness from regional average
            sub.competitiveness = gov.avg_comp_norm[type(firm)]
        else:
            raise ValueError("Firm type not recognized in function add_subsidiary().")

        # Set supplier to best regional capital firm
        sub.supplier = best_cap
        sub.offers = {sub.supplier: sub.supplier.brochure}
        sub.supplier.clients.append(sub)

        # Add subsidiary to firms list and schedule
        self.firms[sub.region][type(sub)].append(sub)
        self.schedule.add(sub)

        gov.new_firms_resources += gov.avg_net_worth
        return sub

    def remove_firm(self, firm: Type[Firm]) -> None:
        """Remove firm from model. """
        self.governments[firm.region].bailout_cost += firm.net_worth
        self.firms[firm.region][type(firm)].remove(firm)
        self.schedule.remove(firm)

    def get_firms(self, region: int, attr=None):
        """Return list of all firms of specified region and type(s).
        
        Args:
            region          : Region where firms are located
        Returns:
            firms           : List of selected firms
        """
        return list(itertools.chain(*(self.firms[region].values())))

    def get_cons_firms(self, region: int):
        """Return list of all consumption (goods and services) firms in this region.

        Args:
            region          : Region where firms are located
        Returns:
            firms           : List of selected firms
        """
        firms = (self.firms[region][ConsumptionGoodFirm] +
                 self.firms[region][ServiceFirm])
        return firms

    def get_firms_by_type(self, firm_type: type, region: int):
        """Return all firms of specified type and region.

        Args:
            region          : Region where firms are located
            type            : Firm type to select
        Returns:
            firms           : List of selected firms
        """
        return self.firms[region][firm_type]

    def get_households(self, region: int):
        """Return list of all households in this region.

        Args:
            region          : Region where household are located
        Returns:
            firms           : List of households
        """
        return self.households[region]

    def step(self) -> None:
        """Defines a single model step in the CRAB model. """

        # -- FLOOD SHOCK -- #
        if self.schedule.time in FLOOD_WHEN.keys():
            self.flood_now = True
            self.flood_return = FLOOD_WHEN[self.schedule.time]
        else:
            self.flood_now = False
            self.flood_return = 0

        # -- MODEL STEP: see stages in agent classes for more details -- #
        self.schedule.step()

        # -- REMOVE BANKRUPT FIRMS -- #
        for region in REGIONS:
            for firm in self.firms_to_remove[region]:
                self.remove_firm(firm)
                # Create new firms as subsidiaries of bankrupt firms
                firm_type = type(firm)
                new_firm = self.add_subsidiary(firm)
            self.governments[region].bailout_cost = 0
            self.governments[region].new_firms_resources = 0

            self.firms_to_remove[region] = []

        # -- OUTPUT COLLECTION -- #
        self.datacollector.collect(self)
